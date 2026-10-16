"""FastMCP server definition: resources + tools + the FastAPI mount.

Design notes
------------
* **Transport.** Streamable-HTTP, mounted into the existing FastAPI app at
  ``/mcp``. We set ``streamable_http_path="/"`` and ``app.mount("/mcp", ...)``
  so the external endpoint is exactly ``/mcp`` (no double ``/mcp/mcp``).
  Compliant MCP clients follow the trailing-slash 307 to ``/mcp/``.

* **Lifespan.** Mounting a sub-app does NOT run its lifespan, so
  ``mcp.session_manager.run()`` is chained into the API lifespan in
  ``api/main.py`` — without it every ``/mcp`` request 500s.

* **Auth.** The endpoint is intentionally NOT added to the API's
  ``_UNAUTHENTICATED_PATHS``; the existing shared-secret middleware gates it,
  so clients pass ``x-api-key: $BACKEND_SHARED_SECRET`` like the UI does.

* **Who is asking.** Every handler answers as the caller the API already
  verified (``api.caller``): the mounted app shares the outer request's
  ``request.state``, so ``caller_gate``'s verdict reaches the handler through
  FastMCP's request context (``_request``). A deployment that fronts ``/mcp``
  with its own sign-in and stamps the signed caller, as the UI proxy does,
  gets per-person answers by the web app's rules: a signed-in email is that
  person (no one when off the roster); open mode with no ``x-caller-email``,
  or an operator, is the principal; a service (signed callers on, no
  assertion) is no one. The principal's own records (episodic memory, the
  activity feed) go to the principal alone; the briefing and roster to a
  rostered caller, the briefing narrowed as ``GET /today`` narrows it; the
  company profile, departments, specialists, knowledge and the workflow
  catalog to anyone who reached ``/mcp``.

* **ask_executive never acts.** Its turn is marked
  ``Session.from_mcp_client``: the chat loop offers and runs no tool that
  sends, posts, books, schedules, runs or changes stored state
  (``schedule_tools.MCP_CLIENT_WITHHELD_TOOLS``), no Act as me or extension
  tool, and of the gateway tools only reads (``MCP_CLIENT_MCP_TOOLS``).

* **DNS-rebinding protection** (FastMCP's Host-header check) is disabled: the
  endpoint authenticates with a header secret (not a cookie), so a browser
  rebinding attack cannot supply credentials, and the server runs behind the
  deployment's TLS terminator. The shared-secret gate is the real access
  control.

* **No prompt-caching impact.** This path is parallel to chat. Resources are
  pure reads; ``consult_specialist`` delegates to ``BaseAgent.analyze`` which
  reuses the existing cached system blocks unchanged.

* **State.** Handlers get no FastAPI ``Request`` argument (only the caller
  read above goes through the request). Most service functions build
  their own SQLite/ChromaDB handles; the one that benefits from the app's warm
  store (``search_knowledge``) reads it from the ``set_store`` singleton.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, get_args

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from openexecutive.api import caller as api_caller

logger = logging.getLogger(__name__)

# Sorted roster of specialist keys, kept in lockstep with
# ``orchestrator/router.SPECIALIST_REGISTRY`` by a unit test. Declared as a
# ``Literal`` so MCP clients see a proper enum in the tool schema.
SpecialistKey = Literal[
    "board_comms", "cfo", "chro", "cmo", "coo", "cpo", "cso", "gc", "sales", "triage"
]

_INSTRUCTIONS = (
    "Open Executive as an MCP server: a company's executive context and a "
    "council of specialist analysts, answered as the person signed in.\n\n"
    "Resources (read-only): the company profile, today's briefing and recent "
    "activity, the people roster, department state, and episodic memory (past "
    "decisions, initiatives, advice). Recent activity and episodic memory are "
    "the owner's alone; the briefing and roster need someone on the roster.\n\n"
    "Tools: `consult_specialist` (domain analysis from one specialist, grounded "
    "in company knowledge), `search_knowledge` (retrieval over the curated "
    "business knowledge base and company documents), `list_workflows` (the "
    "catalog of multi-step executive workflows), and `ask_executive` (one "
    "synthesized answer from the Executive). None of them sends, posts, books "
    "or changes anything."
)

mcp = FastMCP(
    name="open-executive",
    instructions=_INSTRUCTIONS,
    stateless_http=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    ),
)


# ---------------------------------------------------------------------------
# Process-wide store handoff (mirrors mcp_gateway.set_active_gateway).
# ---------------------------------------------------------------------------
_active_store: Any = None


def set_store(store: Any) -> None:
    """Hand the API's warm ChromaDB store to MCP handlers (called from lifespan)."""
    global _active_store
    _active_store = store


def get_store() -> Any:
    """The shared ChromaDB store, or ``None`` (handlers then build their own)."""
    return _active_store


def _json(payload: Any) -> str:
    """Serialize a resource payload as compact JSON, dates → ISO strings."""
    return json.dumps(payload, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Who is asking.
# ---------------------------------------------------------------------------
OWNER_ONLY = "Only the owner can read this."
ROSTER_ONLY = "Only someone on the team can read this."


class CallerRefused(PermissionError):
    """This caller may not read what it asked for. FastMCP returns the
    message to the client as the error."""


@dataclass(frozen=True)
class McpCaller:
    """Who an MCP request asks as: their People entry (None for no one) and
    whether they are the principal, by the web app's rules."""

    person_id: int | None
    is_principal: bool


# No MCP request is in progress: a direct in-process call (tests, scripts).
_NO_MCP_REQUEST = object()


def _request() -> Any:
    """The HTTP request behind the MCP call in progress; None when the call
    has a request context but no HTTP request; ``_NO_MCP_REQUEST`` outside
    any MCP request. Read in the handler's own task, where FastMCP's request
    context is set."""
    try:
        context = mcp.get_context().request_context
    except (LookupError, ValueError):
        return _NO_MCP_REQUEST
    return getattr(context, "request", None)


def _who() -> api_caller.Caller:
    """The verified caller of the request in progress. Outside any MCP
    request (a direct in-process call) it is what a request naming no one
    would be: the principal, or no one once signed callers are on. An MCP
    call that carries no HTTP request names no one (fails closed)."""
    request = _request()
    if request is _NO_MCP_REQUEST:
        return api_caller.Caller("service") if api_caller.signing_on() else api_caller.Caller("open")
    if request is None:
        return api_caller.Caller("service")
    return api_caller.caller(request)


def _resolve(who: api_caller.Caller) -> McpCaller:
    """``who`` on the roster, by ``api.caller.person_id_for``. Fails closed:
    a roster that can't be read makes them no principal."""
    from openexecutive.people.store import is_principal_or_self

    person_id = api_caller.person_id_for(who)
    if who.defaults_to_principal:
        return McpCaller(person_id, True)
    try:
        principal = person_id is not None and is_principal_or_self(person_id, None)
    except Exception:
        logger.warning("mcp_server: principal check failed — answering no", exc_info=True)
        principal = False
    return McpCaller(person_id, principal)


async def current_caller() -> McpCaller:
    """Who the MCP request in progress asks as."""
    who = _who()
    return await asyncio.to_thread(_resolve, who)


async def _require_principal() -> McpCaller:
    resolved = await current_caller()
    if not resolved.is_principal:
        raise CallerRefused(OWNER_ONLY)
    return resolved


async def _require_person() -> McpCaller:
    """The principal, or someone on the roster; a service or an unrostered
    caller names no one, so reads nothing about the people here."""
    resolved = await current_caller()
    if resolved.person_id is None and not resolved.is_principal:
        raise CallerRefused(ROSTER_ONLY)
    return resolved


# ---------------------------------------------------------------------------
# Resources — company-grounded context. Read-only, no LLM calls. The unique
# thing OE has that a generic agent does not.
# ---------------------------------------------------------------------------
@mcp.resource(
    "oe://company/profile",
    name="Company profile",
    description="Structured company profile rendered as a prompt block.",
    mime_type="text/markdown",
)
async def company_profile() -> str:
    from openexecutive.onboarding.profile_builder import load_or_create_profile

    profile = await asyncio.to_thread(load_or_create_profile)
    if profile.is_empty():
        return "No company profile has been configured yet."
    return profile.to_prompt_block()


@mcp.resource(
    "oe://today/briefing",
    name="Today briefing",
    description=(
        "The morning-brief snapshot: department health, roster, proposals. "
        "For someone on the roster, as the web app shows it to them."
    ),
    mime_type="application/json",
)
async def today_briefing() -> str:
    """As ``GET /today`` gives it: the principal's private rows for the
    principal alone, a drafted artifact's card for its owner."""
    from openexecutive.api.routes.today import _build_today
    from openexecutive.orchestrator.artifact_records import viewer_for_person

    who = await _require_person()
    viewer = await asyncio.to_thread(viewer_for_person, who.person_id)
    today = await asyncio.to_thread(
        _build_today, include_private=who.is_principal, viewer=viewer
    )
    today.caller_person_id = who.person_id
    return _json(today.model_dump(mode="json"))


@mcp.resource(
    "oe://today/activity",
    name="Recent activity",
    description="Recent self-initiated Executive activity (last 20 actions). The owner's alone.",
    mime_type="application/json",
)
async def today_activity() -> str:
    from openexecutive.api.routes.today import _build_activity

    await _require_principal()
    activity = await asyncio.to_thread(_build_activity, 20)
    return _json(activity.model_dump(mode="json"))


@mcp.resource(
    "oe://people/roster",
    name="People roster",
    description="Active team members on the company roster. For someone on the roster.",
    mime_type="application/json",
)
async def people_roster() -> str:
    from openexecutive.people.store import list_people

    # The team, never the principal's contacts (as GET /people gives it).
    await _require_person()
    people = await asyncio.to_thread(list_people)
    return _json([p.model_dump(mode="json") for p in people])


@mcp.resource(
    "oe://departments/state",
    name="Department state",
    description="Departments with goals, cadences, and current state.",
    mime_type="application/json",
)
async def departments_state() -> str:
    from openexecutive.departments.store import list_departments

    depts = await asyncio.to_thread(list_departments)
    return _json([d.model_dump(mode="json") for d in depts])


@mcp.resource(
    "oe://memory/decisions",
    name="Episodic memory: decisions",
    description="Past decisions recorded in episodic memory. The owner's alone.",
    mime_type="application/json",
)
async def memory_decisions() -> str:
    from openexecutive.memory.episodic import list_decisions

    await _require_principal()
    rows = await asyncio.to_thread(list_decisions)
    return _json([r.model_dump(mode="json") for r in rows])


@mcp.resource(
    "oe://memory/initiatives",
    name="Episodic memory: initiatives",
    description="Tracked initiatives recorded in episodic memory. The owner's alone.",
    mime_type="application/json",
)
async def memory_initiatives() -> str:
    from openexecutive.memory.episodic import list_initiatives

    await _require_principal()
    rows = await asyncio.to_thread(list_initiatives)
    return _json([r.model_dump(mode="json") for r in rows])


@mcp.resource(
    "oe://memory/advice",
    name="Episodic memory: advice",
    description="Advice given, recorded in episodic memory. The owner's alone.",
    mime_type="application/json",
)
async def memory_advice() -> str:
    from openexecutive.memory.episodic import list_advice

    await _require_principal()
    rows = await asyncio.to_thread(list_advice)
    return _json([r.model_dump(mode="json") for r in rows])


# ---------------------------------------------------------------------------
# Tools — structured capabilities. None of them sends, posts, books or
# changes anything; ask_executive records the conversation in memory.
# ---------------------------------------------------------------------------
_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


@mcp.tool(title="Consult a specialist", annotations=_READ_ONLY)
async def consult_specialist(
    specialist: SpecialistKey, query: str, context: str = ""
) -> str:
    """Consult one of Open Executive's specialist executives for domain analysis.

    Each specialist runs its own knowledge retrieval and returns a grounded,
    domain-expert read. Specialists: cso (strategy/M&A/OKRs), cfo (finance/unit
    economics/fundraising), chro (people/comp/org design), gc (legal/contracts/
    compliance), coo (operations/process/metrics), cmo (GTM/brand/PR), cpo
    (product/roadmap), sales (pipeline/deals/pricing conversations/forecast),
    board_comms (board decks/IR/governance),
    triage (chief of staff — significance of inbound events).

    Args:
        specialist: Which specialist to consult.
        query: The specific question or task. The specialist sees only this
            query plus ``context``, so be precise.
        context: Relevant background from your own task to ground the answer.
    """
    from openexecutive.orchestrator.router import (
        SPECIALIST_REGISTRY,
        route_to_specialist,
    )

    if specialist not in SPECIALIST_REGISTRY:
        valid = ", ".join(sorted(SPECIALIST_REGISTRY))
        raise ValueError(f"Unknown specialist {specialist!r}. Valid: {valid}")
    return await route_to_specialist(
        specialist, query, context=context, actor="specialist_mcp",
    )


@mcp.tool(title="Search knowledge", annotations=_READ_ONLY)
async def search_knowledge(query: str) -> str:
    """Search Open Executive's curated business knowledge base and indexed company docs.

    Returns the most relevant retrieved chunks, each prefixed with its
    ``[source]``. No LLM call: raw retrieval to cite or reason over.
    """
    from openexecutive.knowledge.retriever import retrieve

    return await asyncio.to_thread(retrieve, query=query, store=get_store())


@mcp.tool(title="List workflows", annotations=_READ_ONLY)
async def list_workflows() -> str:
    """List the available multi-step executive workflows and their input schemas.

    Returns a JSON array of workflow metadata (name, title, description,
    estimated minutes, input schema, steps). Workflows are run from Open
    Executive itself, not over MCP.
    """
    from openexecutive.workflows import list_workflows as _list

    workflows = await asyncio.to_thread(_list)
    return _json([w.meta().model_dump(mode="json") for w in workflows])


@mcp.tool(
    title="Ask the Executive",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def ask_executive(message: str) -> str:
    """Ask the Executive a question and get one synthesized, cross-domain answer.

    The Executive answers as it would in Open Executive for the person signed
    in, drawing on the specialists, company knowledge and memory. It reads and
    advises only: it does not send, post, book or change anything. The
    conversation is recorded in Open Executive's memory. Non-streaming.

    Args:
        message: The question for the Executive.
    """
    from openexecutive.orchestrator.artifact_records import NOBODY
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway
    from openexecutive.orchestrator.session import Session

    # Who is asking comes from the verified request, never from the message:
    # a signed-in person, the principal for a request that names no one in
    # open mode, no one for a service.
    who = _who()
    person_id = await asyncio.to_thread(api_caller.person_id_for, who)

    from openexecutive.onboarding.profile_builder import load_or_create_profile

    profile = await asyncio.to_thread(load_or_create_profile)
    session = Session(
        company_profile=profile if not profile.is_empty() else None,
        # A client that resolves to no one reads and publishes no one's
        # documents (``artifact_records.current_viewer`` would otherwise take
        # a speakerless session for the principal's own work).
        documents_viewer=NOBODY if person_id is None else None,
        # Another AI app is asking: the turn reads and advises but never
        # sends, posts, books or changes anything (Session.from_mcp_client).
        from_mcp_client=True,
        caller_person_id=person_id,
    )

    executive = Executive(mcp_gateway=get_active_gateway())
    return await executive.chat(
        user_message=message, session=session, person_id=person_id
    )


# ---------------------------------------------------------------------------
# Mount.
# ---------------------------------------------------------------------------
_http_app: Any = None


def mount(app: Any) -> None:
    """Attach the Streamable-HTTP MCP app to a FastAPI app at ``/mcp``.

    The first call builds the Streamable-HTTP ASGI app, which also lazily
    creates ``mcp.session_manager`` (run by the API lifespan — see
    ``api/main.py``). The result is cached so repeated ``create_app()`` calls
    reuse the *same* sub-app and session manager rather than orphaning the one
    the lifespan will run.
    """
    global _http_app
    if _http_app is None:
        _http_app = mcp.streamable_http_app()
    app.mount("/mcp", _http_app)
    logger.info("mcp_server mounted at /mcp")


_session_started = False


@asynccontextmanager
async def run_session_manager() -> AsyncIterator[None]:
    """Run the Streamable-HTTP session manager for the app's serving phase.

    ``StreamableHTTPSessionManager.run()`` can be entered **only once per
    instance**, and FastMCP caches a single manager on the module-global ``mcp``.
    Production starts one app once, so this runs the manager for the app's life.
    The guard makes it a no-op on any *subsequent* lifespan entry in the same
    process — which only happens in the test suite, where apps are rebuilt via
    ``create_app()`` (a test that drives the live ``/mcp`` protocol gives the
    endpoint a session manager of its own) — instead of raising
    ``RuntimeError``.
    """
    global _session_started
    if _session_started:
        yield
        return
    _session_started = True
    async with mcp.session_manager.run():
        yield


def specialist_keys() -> tuple[str, ...]:
    """The specialist enum advertised in the ``consult_specialist`` schema.

    Exposed for the drift test that pins it to ``SPECIALIST_REGISTRY``.
    """
    return get_args(SpecialistKey)
