"""Every model call site records a ``cache_event`` usage row: specialist
consults, specialist tool calls, triage, the chat memory extractor, the
brief and its grounding repair, and alert review (the Executive's own turns
and the research loops are covered by their module tests), plus a guard that
fails any model call in the package that records no row."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import openexecutive
from openexecutive.agents.base import BaseAgent
from openexecutive.audit.logger import AuditLogger, set_audit_logger


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    logger = AuditLogger(tmp_path / "audit.db")
    set_audit_logger(logger)
    yield logger  # type: ignore[misc]
    set_audit_logger(None)


def _response(**usage: object) -> SimpleNamespace:
    return SimpleNamespace(
        content=[], stop_reason="end_turn", usage=SimpleNamespace(**usage),
    )


class _Agent(BaseAgent):
    name = "unit_agent"
    domain = "unit"
    model = "claude-test"

    def get_system_prompt(self) -> str:
        return "prompt"


def test_analyze_with_tools_records_usage_under_the_callers_actor(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("openexecutive.agents.overrides.get_override", lambda _name: None)
    provider = SimpleNamespace(messages_create=AsyncMock(return_value=_response(
        input_tokens=70, output_tokens=9,
        server_tool_use=SimpleNamespace(web_search_requests=3),
    )))
    monkeypatch.setattr("openexecutive.agents.base.get_provider", lambda _m: provider)

    asyncio.run(_Agent().analyze_with_tools(
        "ctx", tools=[{"name": "t", "input_schema": {"type": "object"}}],
        model_override="claude-research", actor="specialist_research",
    ))
    rows = audit.query(event_type="cache_event")
    assert len(rows) == 1
    assert rows[0].actor == "specialist_research"
    assert rows[0].details["model"] == "claude-research"
    assert rows[0].details["web_search_requests"] == 3


def test_analyze_records_usage_as_specialist(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat-turn consult (``consult_specialist`` → ``analyze``) writes one
    usage row under the default ``specialist`` actor, so the session cost
    summary and ``/audit/usage`` count it."""
    monkeypatch.setattr("openexecutive.agents.overrides.get_override", lambda _name: None)
    response = _response(input_tokens=120, output_tokens=40, cache_read_input_tokens=100)
    response.content = [SimpleNamespace(type="text", text="analysis")]
    provider = SimpleNamespace(messages_create=AsyncMock(return_value=response))
    monkeypatch.setattr("openexecutive.agents.base.get_provider", lambda _m: provider)

    text = asyncio.run(_Agent().analyze("What should we do?"))

    assert text == "analysis"
    rows = audit.query(event_type="cache_event")
    assert len(rows) == 1
    assert rows[0].actor == "specialist"
    assert rows[0].details["model"] == "claude-test"
    assert rows[0].details["input_tokens"] == 120
    assert rows[0].details["output_tokens"] == 40
    assert rows[0].details["cache_read_input_tokens"] == 100


def test_analyze_records_usage_under_an_explicit_actor(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Council test box passes ``actor="agent_test"`` so sandbox runs
    are separable from production consults in the by-source breakdown."""
    monkeypatch.setattr("openexecutive.agents.overrides.get_override", lambda _name: None)
    response = _response(input_tokens=5, output_tokens=1)
    response.content = [SimpleNamespace(type="text", text="ok")]
    provider = SimpleNamespace(messages_create=AsyncMock(return_value=response))
    monkeypatch.setattr("openexecutive.agents.base.get_provider", lambda _m: provider)

    asyncio.run(_Agent().analyze("q", actor="agent_test"))

    rows = audit.query(event_type="cache_event")
    assert [r.actor for r in rows] == ["agent_test"]


def test_route_to_specialist_tags_rows_with_the_active_turn(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct router call (a workflow step) records under the
    ``specialist_workflow`` actor and inherits the active audit turn, which
    is what lets ``/audit/sessions/{id}`` group the row under its turn."""
    from openexecutive.audit.context import set_turn
    from openexecutive.orchestrator import router

    monkeypatch.setattr("openexecutive.agents.overrides.get_override", lambda _name: None)
    monkeypatch.setitem(router.SPECIALIST_REGISTRY, "unit_agent", _Agent())
    response = _response(input_tokens=8, output_tokens=3)
    response.content = [SimpleNamespace(type="text", text="ok")]
    provider = SimpleNamespace(messages_create=AsyncMock(return_value=response))
    monkeypatch.setattr("openexecutive.agents.base.get_provider", lambda _m: provider)

    with set_turn(session_id="s-1", turn_id="t-1"):
        asyncio.run(router.route_to_specialist("unit_agent", "q"))

    rows = audit.query(event_type="cache_event")
    assert len(rows) == 1
    assert rows[0].actor == "specialist_workflow"
    assert rows[0].session_id == "s-1"
    assert rows[0].turn_id == "t-1"


def test_executive_test_box_records_usage(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Council's Executive preview is a raw provider call; it records
    under ``agent_test`` like the specialist test box."""
    from openexecutive.api.routes.agents import AgentTestRequest, _test_executive

    monkeypatch.setattr("openexecutive.api.routes.agents.get_override", lambda _name: None)
    response = _response(input_tokens=9, output_tokens=2)
    response.content = [SimpleNamespace(type="text", text="preview")]
    provider = SimpleNamespace(messages_create=AsyncMock(return_value=response))
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: provider)

    text = asyncio.run(_test_executive(AgentTestRequest(query="hi", prompt="P", model="claude-x")))

    assert text == "preview"
    rows = audit.query(event_type="cache_event")
    assert [r.actor for r in rows] == ["agent_test"]
    assert rows[0].details["model"] == "claude-x"


def test_triage_records_usage(audit: AuditLogger) -> None:
    from openexecutive.agents.triage import TriageAgent
    from openexecutive.alerts.models import AlertEvent

    block = SimpleNamespace(type="tool_use", name="emit_alert_decision", input={
        "alert": False, "severity": "low", "channels": ["persisted"], "headline": "h",
        "body": "b", "suggested_action": "", "topic_tags": [], "dedup_key": "k",
        "reason_if_suppressed": "noise",
    })
    response = SimpleNamespace(content=[block], stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=33, output_tokens=5))
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
    asyncio.run(TriageAgent().triage(
        AlertEvent(source="email", external_id="m1", subject="s", body="b"), client=client,
    ))
    rows = audit.query(event_type="cache_event")
    assert len(rows) == 1 and rows[0].actor == "triage"
    assert rows[0].details["input_tokens"] == 33


def test_memory_extractor_records_usage(
    audit: AuditLogger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.memory import episodic as ep

    db = tmp_path / "episodic.db"
    ep.initialize_db(db)
    provider = SimpleNamespace(messages_create=AsyncMock(return_value=_response(
        input_tokens=12, output_tokens=2,
    )))
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: provider)
    monkeypatch.setattr(
        "openexecutive.config.get_settings", lambda: SimpleNamespace(routing_model="claude-test"),
    )
    asyncio.run(ep.extract_and_store("user message", "assistant response", db_path=db))
    rows = audit.query(event_type="cache_event")
    assert len(rows) == 1 and rows[0].actor == "memory_extractor"
    assert rows[0].details["model"] == "claude-test"


def test_memory_extractor_retry_records_its_own_usage(
    audit: AuditLogger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The corrective pass is a second model call and must show up as one."""
    from openexecutive.memory import episodic as ep

    db = tmp_path / "episodic.db"
    ep.initialize_db(db)
    bad_quote = SimpleNamespace(
        type="tool_use", name="store_memories",
        input={"decisions": [{
            "summary": "Cut burn", "user_commitment_quote": "we will cut the burn",
        }]},
    )
    first = _response(input_tokens=12, output_tokens=2)
    first.content = [bad_quote]
    provider = SimpleNamespace(messages_create=AsyncMock(side_effect=[
        first, _response(input_tokens=30, output_tokens=1),
    ]))
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: provider)
    monkeypatch.setattr(
        "openexecutive.config.get_settings", lambda: SimpleNamespace(routing_model="claude-test"),
    )
    asyncio.run(ep.extract_and_store("Cut burn to 400k.", "ok", db_path=db))
    rows = audit.query(event_type="cache_event")
    assert len(rows) == 2 and {r.actor for r in rows} == {"memory_extractor"}
    assert sorted(r.details["input_tokens"] for r in rows) == [12, 30]


def _text_response(text: str, **usage: object) -> SimpleNamespace:
    response = _response(**usage)
    response.content = [SimpleNamespace(type="text", text=text)]
    return response


@pytest.fixture
def fast_provider(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """A utility-fast provider answering with one text block."""
    create = AsyncMock(return_value=_text_response("brief", input_tokens=50, output_tokens=7))
    monkeypatch.setattr(
        "openexecutive.providers.get_provider",
        lambda _m: SimpleNamespace(messages_create=create),
    )
    monkeypatch.setattr("openexecutive.agents.utility_fast.get_fast_model", lambda: "claude-fast")
    return create


def test_briefing_narrative_records_usage(
    audit: AuditLogger, fast_provider: AsyncMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The morning brief and the /today header both synthesize through here,
    on the Executive's model."""
    from openexecutive.briefing.narrative import synthesize_briefing_narrative

    monkeypatch.setattr("openexecutive.briefing.narrative.brief_model", lambda: "claude-exec")

    text = asyncio.run(synthesize_briefing_narrative(
        today_data={}, activity=[], period_label="Monday", rendered_context="ctx",
    ))

    assert text == "brief"
    rows = audit.query(event_type="cache_event")
    assert [r.actor for r in rows] == ["briefing_narrative"]
    assert rows[0].details["model"] == "claude-exec"
    assert rows[0].details["input_tokens"] == 50


def test_briefing_grounding_repair_records_usage(
    audit: AuditLogger, fast_provider: AsyncMock,
) -> None:
    from openexecutive.briefing.grounding import GroundingReport, _repair

    asyncio.run(_repair("draft", "context", "system", GroundingReport(lines=[])))

    rows = audit.query(event_type="cache_event")
    assert [r.actor for r in rows] == ["briefing_grounding"]
    assert rows[0].details["output_tokens"] == 7


def test_alert_review_records_usage(audit: AuditLogger, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.agents.alert_review import AlertReviewAgent

    monkeypatch.setattr("openexecutive.agents.overrides.get_override", lambda _name: None)
    block = SimpleNamespace(type="tool_use", name="emit_alert_reviews", input={"reviews": []})
    response = _response(input_tokens=400, output_tokens=60, cache_read_input_tokens=300)
    response.content = [block]
    provider = SimpleNamespace(messages_create=AsyncMock(return_value=response))
    monkeypatch.setattr("openexecutive.agents.alert_review.get_provider", lambda _m: provider)

    agent = AlertReviewAgent()
    asyncio.run(agent.review("batch"))

    rows = audit.query(event_type="cache_event")
    assert [r.actor for r in rows] == ["alert_review"]
    assert rows[0].details["model"] == agent.effective_model()
    assert rows[0].details["cache_read_input_tokens"] == 300


def test_alert_review_failed_call_records_nothing(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call that raised has no response to read usage from."""
    from openexecutive.agents.alert_review import AlertReviewAgent

    monkeypatch.setattr("openexecutive.agents.overrides.get_override", lambda _name: None)
    provider = SimpleNamespace(messages_create=AsyncMock(side_effect=RuntimeError("down")))
    monkeypatch.setattr("openexecutive.agents.alert_review.get_provider", lambda _m: provider)

    assert asyncio.run(AlertReviewAgent().review("batch")) == []
    assert audit.query(event_type="cache_event") == []


PACKAGE = Path(openexecutive.__file__).resolve().parent

# Modules exempt from the guard below, each with the reason. Keep this short:
# an entry is a place the usage view can silently under-count.
_EXEMPT: dict[str, str] = {
    "architecture/generator.py": (
        "no runtime caller: architecture/__init__.py exports it for re-authoring, "
        "but the /architecture page serves the hand-authored prebuilt JSON and "
        "nothing calls generate_section outside tests"
    ),
}


def _is_model_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in {"messages_create", "messages_stream"}:
        return True
    # A raw SDK client (triage's injectable one) spells it messages.create.
    return (
        func.attr in {"create", "stream"}
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "messages"
    )


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


_FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)


def _own_calls(fn: ast.AST) -> list[ast.Call]:
    """The calls in ``fn``'s own body — a nested def is its own function."""
    calls: list[ast.Call] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (*_FUNCTION, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Call):
            calls.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return calls


def _unlogged_model_calls(tree: ast.Module) -> list[tuple[int, str]]:
    """``(line, function)`` for each model call whose function records no
    usage. A function records usage when it calls ``log_model_usage``, or a
    function of the same module that does (the Executive's
    ``_emit_cache_event``), or is nested in one that does (a retry closure
    whose caller logs each response).

    The check is per function and by name, so a green run is not proof:
    it misses a call at module level or in a lambda, a call through
    ``getattr`` or an alias, and a second model call in a function that
    logs one, and it lets two same-named functions in a module share one
    logging status."""
    loggers = {"log_model_usage"} | {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, _FUNCTION)
        and any(_called_name(c) == "log_model_usage" for c in _own_calls(fn))
    }
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST, enclosing_logs: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, _FUNCTION):
                visit(child, enclosing_logs)
                continue
            calls = _own_calls(child)
            logs = enclosing_logs or any(_called_name(c) in loggers for c in calls)
            if not logs:
                found.extend((c.lineno, child.name) for c in calls if _is_model_call(c))
            visit(child, logs)

    visit(tree, False)
    return found


def test_every_model_call_records_usage() -> None:
    """Every model call in the ``openexecutive`` package records a
    ``cache_event`` row, so ``/audit/usage`` and the session cost view count
    it. A new call site that skips ``log_model_usage`` fails here instead of
    quietly under-counting. The providers implement the call and are
    skipped; the repo-root ``evals/`` harness is a separate client process
    and out of scope."""
    offenders = [
        f"{rel}:{line} ({name})"
        for path in sorted(PACKAGE.rglob("*.py"))
        if not (rel := path.relative_to(PACKAGE).as_posix()).startswith("providers/")
        and rel not in _EXEMPT
        for line, name in _unlogged_model_calls(ast.parse(path.read_text(), filename=str(path)))
    ]
    assert offenders == []
    assert sorted(m for m in _EXEMPT if not (PACKAGE / m).exists()) == []


def test_the_usage_guard_catches_an_unlogged_call() -> None:
    """Negative control: the guard flags a bare call and accepts each logged
    shape, so a clean run above means something."""
    source = (
        "async def bare(p):\n"
        "    return await p.messages_create(model='m')\n"
        "async def direct(p):\n"
        "    r = await p.messages_create(model='m')\n"
        "    log_model_usage(r, model='m', actor='a')\n"
        "def _wrapper(r):\n"
        "    log_model_usage(r, model='m', actor='a')\n"
        "async def via_wrapper(p):\n"
        "    async with p.messages_stream(model='m') as s:\n"
        "        _wrapper(await s.get_final_message())\n"
        "async def outer(p):\n"
        "    async def _call():\n"
        "        return await p.messages_create(model='m')\n"
        "    log_model_usage(await _call(), model='m', actor='a')\n"
        "async def raw(client):\n"
        "    return await client.messages.create(model='m')\n"
    )
    assert _unlogged_model_calls(ast.parse(source)) == [(2, "bare"), (16, "raw")]


