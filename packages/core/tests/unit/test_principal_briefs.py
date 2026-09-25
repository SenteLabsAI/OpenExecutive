"""Tests for the morning_brief / end_of_day_digest workflows and the
scheduler-side seeding + chain-next plumbing.

The workflow LLM calls are stubbed — we verify the registration,
input model shape, scheduler seeding idempotency, time-of-day parsing,
and the chain-on-fire behaviour. Actual LLM-rendered content is out
of scope for unit tests.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from openexecutive.memory import episodic
from openexecutive.scheduler import runner
from openexecutive.workflows import WORKFLOW_REGISTRY


def _setup_isolated_db(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)


@pytest.fixture(autouse=True)
def _no_default_audit_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """The brief handler audits every outcome; keep those rows out of the
    default ./episodic_memory.db, where they leak into other modules."""
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# Workflow registration
# ---------------------------------------------------------------------------

def test_morning_brief_registered() -> None:
    assert "morning_brief" in WORKFLOW_REGISTRY
    wf = WORKFLOW_REGISTRY["morning_brief"]
    assert wf.title
    assert wf.description
    assert len(wf.steps()) >= 1
    meta = wf.meta()
    # Input model has at least the period_label field, all optional.
    assert "period_label" in meta.input_schema.get("properties", {})


def test_end_of_day_digest_registered() -> None:
    assert "end_of_day_digest" in WORKFLOW_REGISTRY
    wf = WORKFLOW_REGISTRY["end_of_day_digest"]
    assert wf.title
    meta = wf.meta()
    assert "period_label" in meta.input_schema.get("properties", {})


# ---------------------------------------------------------------------------
# Time-of-day parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("08:00", (8, 0)),
        ("18:30", (18, 30)),
        ("00:00", (0, 0)),
        ("23:59", (23, 59)),
        ("", (8, 0)),  # falls back to default
        ("not-a-time", (8, 0)),
        ("25:00", (8, 0)),  # out of range
        ("12:60", (8, 0)),  # minutes out of range
    ],
)
def test_parse_hhmm(raw: str, expected: tuple[int, int]) -> None:
    assert runner._parse_hhmm(raw, "08:00") == expected


def test_next_occurrence_picks_today_when_target_is_later() -> None:
    base = datetime(2026, 5, 26, 7, 0, tzinfo=UTC)
    result = runner._next_occurrence(base, 8, 0)
    assert result == datetime(2026, 5, 26, 8, 0, tzinfo=UTC)


def test_next_occurrence_advances_to_tomorrow_when_target_passed() -> None:
    base = datetime(2026, 5, 26, 9, 0, tzinfo=UTC)
    result = runner._next_occurrence(base, 8, 0)
    assert result == datetime(2026, 5, 27, 8, 0, tzinfo=UTC)


def test_next_occurrence_advances_when_equal_to_now() -> None:
    """Exactly at target time should advance to tomorrow — strictly future."""
    base = datetime(2026, 5, 26, 8, 0, tzinfo=UTC)
    result = runner._next_occurrence(base, 8, 0)
    assert result == datetime(2026, 5, 27, 8, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Scheduler seeding idempotency
# ---------------------------------------------------------------------------

def test_seed_principal_briefs_inserts_all_recurring_rows_on_fresh_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shift 5 added `executive_reflection` to the seed loop alongside
    the morning and EoD brief. Fresh DB → 3 rows."""
    _setup_isolated_db(tmp_path / "briefs.db", monkeypatch)

    inserted = runner.seed_principal_briefs()
    assert inserted == 3

    actions = episodic.list_scheduled_actions(status="pending", limit=10)
    kinds = {a.kind for a in actions}
    assert "principal_brief_morning" in kinds
    assert "principal_brief_eod" in kinds
    assert "executive_reflection" in kinds


def test_seed_principal_briefs_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_db(tmp_path / "briefs2.db", monkeypatch)
    runner.seed_principal_briefs()

    # Second call must not duplicate.
    inserted = runner.seed_principal_briefs()
    assert inserted == 0
    pending = [
        a for a in episodic.list_scheduled_actions(status="pending", limit=10)
        if a.kind in (
            "principal_brief_morning",
            "principal_brief_eod",
            "executive_reflection",
        )
    ]
    # Shift 5 added executive_reflection; the idempotency contract now
    # covers all three.
    assert len(pending) == 3


def test_seed_uses_env_var_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When PRINCIPAL_BRIEF_MORNING_TIME is set, the seeded row uses that
    HH:MM rather than the 08:00 default."""
    _setup_isolated_db(tmp_path / "briefs3.db", monkeypatch)
    monkeypatch.setenv("PRINCIPAL_BRIEF_MORNING_TIME", "06:30")
    monkeypatch.setenv("PRINCIPAL_BRIEF_EOD_TIME", "17:15")

    runner.seed_principal_briefs()
    actions = {
        a.kind: a for a in episodic.list_scheduled_actions(status="pending", limit=10)
    }
    morning = actions["principal_brief_morning"]
    eod = actions["principal_brief_eod"]
    # Parse run_at and check HH:MM matches the env vars.
    morning_dt = datetime.fromisoformat(morning.run_at)
    eod_dt = datetime.fromisoformat(eod.run_at)
    assert (morning_dt.hour, morning_dt.minute) == (6, 30)
    assert (eod_dt.hour, eod_dt.minute) == (17, 15)


# ---------------------------------------------------------------------------
# Chain-next on fire
# ---------------------------------------------------------------------------

def test_enqueue_next_principal_brief_advances_24h(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a brief fires at noon, the next occurrence must be tomorrow at
    the configured time-of-day — not 24h literal from now."""
    _setup_isolated_db(tmp_path / "briefs4.db", monkeypatch)
    monkeypatch.setenv("PRINCIPAL_BRIEF_MORNING_TIME", "08:00")

    # Fire-time is "after" — well past today's 08:00.
    after = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
    aid = runner._enqueue_next_principal_brief("principal_brief_morning", after=after)
    assert aid is not None

    row = episodic.get_scheduled_action(aid)
    assert row is not None
    next_dt = datetime.fromisoformat(row.run_at)
    # Strictly after the fire-time AND at 08:00 UTC.
    assert next_dt > after
    assert next_dt.hour == 8 and next_dt.minute == 0


def test_has_pending_brief_detects_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_db(tmp_path / "briefs5.db", monkeypatch)
    assert runner._has_pending_brief("principal_brief_morning") is False
    runner.seed_principal_briefs()
    assert runner._has_pending_brief("principal_brief_morning") is True
    assert runner._has_pending_brief("principal_brief_eod") is True


# ---------------------------------------------------------------------------
# Delivered-brief state: the runner records the fingerprint only on delivery
# ---------------------------------------------------------------------------


def _fake_brief_workflow(fingerprint: str, artifact: str = "BRIEF"):
    from openexecutive.workflows.base import WorkflowEvent
    from openexecutive.workflows.morning_brief import MorningBriefInput, MorningBriefWorkflow

    class _Fake(MorningBriefWorkflow):
        async def run(self, inputs, store):  # type: ignore[override]
            yield WorkflowEvent(type="result", data={"brief_fingerprint": fingerprint, "suppressed": False})
            yield WorkflowEvent(type="artifact", content=artifact)
            yield WorkflowEvent(type="done")

        def input_model(self):  # type: ignore[override]
            return MorningBriefInput

    return _Fake()


def _run_brief(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    deliver_ok: bool = True,
    workflow: object | None = None,
    deliver: object | None = None,
) -> None:
    import asyncio

    from openexecutive.briefing import narrative_cache
    from openexecutive.workflows import persistence as wf_persistence

    db = tmp_path / "brief.db"
    _setup_isolated_db(db, monkeypatch)
    monkeypatch.setattr(narrative_cache, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(wf_persistence, "DB_PATH", db)
    monkeypatch.setitem(
        WORKFLOW_REGISTRY, "morning_brief", workflow or _fake_brief_workflow("fp-123")
    )

    async def _deliver(text: str, **_kw: object) -> runner.PrincipalDelivery:
        if deliver_ok:
            return runner.PrincipalDelivery(True, "discord_dm → 1", "delivered", "discord_dm")
        return runner.PrincipalDelivery(False, "delivery failed", "send_failed")

    monkeypatch.setattr(runner, "_deliver_to_principal", deliver or _deliver)
    monkeypatch.setattr(runner, "_enqueue_next_principal_brief", lambda kind, after: None)

    class _Store:
        def __init__(self, **kw): ...

    import openexecutive.knowledge.store as kstore

    monkeypatch.setattr(kstore, "ChromaDBStore", _Store)

    action_id = episodic.insert_scheduled_action(
        run_at=datetime.now(UTC).isoformat(), channel="__internal__", channel_ref="principal",
        intent_text="brief", kind="principal_brief_morning",
    )
    action = episodic.get_scheduled_action(action_id)
    assert action is not None
    asyncio.run(runner._run_principal_brief(action, datetime.now(UTC)))


def test_run_principal_brief_records_fingerprint_after_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.briefing import brief_state

    _run_brief(tmp_path, monkeypatch, deliver_ok=True)
    last = brief_state.last_delivered("principal_brief_morning")
    assert last is not None and last.input_hash == "fp-123" and last.narrative_text == "BRIEF"
    outcome = brief_state.last_delivery_outcome()
    assert outcome is not None
    assert (outcome.kind, outcome.reason, outcome.channel) == (
        "principal_brief_morning", "delivered", "discord_dm",
    )


def test_run_principal_brief_does_not_record_on_delivery_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.briefing import brief_state

    _run_brief(tmp_path, monkeypatch, deliver_ok=False)
    assert brief_state.last_delivered("principal_brief_morning") is None
    # The failure is still recorded, for the Briefing's "not sent" notice.
    outcome = brief_state.last_delivery_outcome()
    assert outcome is not None and (outcome.reason, outcome.channel) == ("send_failed", None)


def _failing_brief_workflow(event: str):  # type: ignore[no-untyped-def]
    from openexecutive.workflows.base import WorkflowEvent
    from openexecutive.workflows.morning_brief import MorningBriefInput, MorningBriefWorkflow

    class _Fails(MorningBriefWorkflow):
        async def run(self, inputs, store):  # type: ignore[override]
            if event == "error":
                yield WorkflowEvent(type="error", message="model unavailable")
            yield WorkflowEvent(type="done")  # "empty": no artifact at all

        def input_model(self):  # type: ignore[override]
            return MorningBriefInput

    return _Fails()


@pytest.mark.parametrize("event", ["error", "empty"])
def test_a_brief_that_couldnt_be_written_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    from openexecutive.briefing import brief_state

    sends: list[str] = []

    async def _deliver(text: str, **_kw: object) -> runner.PrincipalDelivery:
        sends.append(text)
        return runner.PrincipalDelivery(True, "email → x", "delivered", "email")

    _run_brief(tmp_path, monkeypatch, workflow=_failing_brief_workflow(event), deliver=_deliver)
    assert sends == []
    outcome = brief_state.last_delivery_outcome()
    assert outcome is not None and outcome.reason == "not_written"


def test_a_send_that_crashes_is_recorded_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.briefing import brief_state

    async def _crash(text: str, **_kw: object) -> runner.PrincipalDelivery:
        raise RuntimeError("people store locked")

    _run_brief(tmp_path, monkeypatch, deliver=_crash)
    outcome = brief_state.last_delivery_outcome()
    assert outcome is not None and outcome.reason == "send_failed"


# ---------------------------------------------------------------------------
# Delivery: preferred channel honoured, email via the MCP gateway, and no
# synthesis when nothing can reach the principal
# ---------------------------------------------------------------------------


class _Sent:
    """Records every outbound send the delivery path makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []  # type: ignore[type-arg]


class _Gateway:
    def __init__(self, sent: _Sent, result: str = "Email sent! Message ID: m-1") -> None:
        self._sent = sent
        self._result = result

    async def call_tool(self, tool_input: dict) -> str:  # type: ignore[type-arg]
        self._sent.calls.append(("gmail", tool_input))
        return self._result


@pytest.fixture
def sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Sent:
    """Isolated people DB, recording chat send handlers, no MCP gateway."""
    import json

    from openexecutive.orchestrator import mcp_gateway, schedule_tools
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    db = tmp_path / "people.db"
    _setup_isolated_db(db, monkeypatch)
    monkeypatch.setattr(people_store, "DB_PATH", db)
    people_store.initialize_db(db)
    people_registry.invalidate()
    monkeypatch.setattr(mcp_gateway, "_active_gateway", None)

    rec = _Sent()

    def _handler(name: str):  # type: ignore[no-untyped-def]
        async def _send(args: dict) -> str:  # type: ignore[type-arg]
            rec.calls.append((name, args))
            return json.dumps({"status": "sent"})
        return _send

    monkeypatch.setattr(schedule_tools, "handle_send_slack_dm", _handler("slack"))
    monkeypatch.setattr(schedule_tools, "handle_send_discord_dm", _handler("discord"))
    monkeypatch.setattr(schedule_tools, "handle_send_telegram_message", _handler("telegram"))
    return rec


def _principal(**fields: object) -> int:
    from openexecutive.people import store as people_store

    return people_store.upsert_person(full_name="Owner", is_principal=True, **fields)  # type: ignore[arg-type]


def _with_gateway(monkeypatch: pytest.MonkeyPatch, sent: _Sent, **kw: str) -> None:
    """An MCP gateway running the Google Workspace server, so email is ready."""
    from openexecutive.orchestrator import mcp_gateway

    monkeypatch.setattr(mcp_gateway, "_active_gateway", _Gateway(sent, **kw))
    monkeypatch.setattr(mcp_gateway, "configured_server_names", lambda _path: ["google_workspace"])


def _deliver(text: str = "BRIEF", label: str = "Morning Brief") -> tuple[bool, str]:
    import asyncio

    result = asyncio.run(runner._deliver_to_principal(text, label=label))
    return result.ok, result.detail


def test_preferred_slack_is_honoured(sent: _Sent) -> None:
    """`preferred_channel` is "slack", not "slack_dm" — it used to match nothing."""
    _principal(
        preferred_channel="slack", slack_user_id="U1", discord_user_id="D1",
        telegram_chat_id="42",
    )
    ok, detail = _deliver()
    assert ok and detail == "slack_dm → U1"
    assert [name for name, _ in sent.calls] == ["slack"]


def test_preferred_discord_goes_ahead_of_slack(sent: _Sent) -> None:
    _principal(preferred_channel="discord", slack_user_id="U1", discord_user_id="D1")
    ok, _ = _deliver()
    assert ok
    assert [name for name, _ in sent.calls] == ["discord"]


def test_preferred_email_sends_through_the_gateway(
    sent: _Sent, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.config import get_settings

    _principal(preferred_channel="email", email="owner@example.com", slack_user_id="U1")
    _with_gateway(monkeypatch, sent)

    ok, detail = _deliver("**the** brief", label="Morning Brief")

    assert ok and detail == "email → owner@example.com"
    ((name, call),) = sent.calls
    assert name == "gmail"
    assert call["name"] == "google_workspace__send_gmail_message"
    args = call["arguments"]
    assert args["user_google_email"] == get_settings().exec_email_address
    assert args["to"] == "owner@example.com"
    assert args["subject"] == runner._email_subject("Morning Brief")
    # Formatted, not the raw Markdown.
    assert args["body_format"] == "html"
    assert "<strong>the</strong> brief" in args["body"] and "**" not in args["body"]


def test_email_error_falls_back_to_chat(sent: _Sent, monkeypatch: pytest.MonkeyPatch) -> None:
    _principal(preferred_channel="email", email="owner@example.com", slack_user_id="U1")
    _with_gateway(monkeypatch, sent, result='{"error": "recipient not on the roster"}')

    ok, detail = _deliver()

    assert ok and detail == "slack_dm → U1"
    assert [name for name, _ in sent.calls] == ["gmail", "slack"]


def test_any_with_only_an_email_uses_email(sent: _Sent, monkeypatch: pytest.MonkeyPatch) -> None:
    _principal(email="owner@example.com")  # preferred_channel defaults to "any"
    _with_gateway(monkeypatch, sent)

    ok, detail = _deliver()

    assert ok and detail == "email → owner@example.com"


def test_any_with_a_chat_channel_never_emails(
    sent: _Sent, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An owner linked by email at setup (preference "any") who also has Slack
    must not get the briefs by email too while Slack works."""
    _principal(email="owner@example.com", slack_user_id="U1")
    _with_gateway(monkeypatch, sent)

    ok, detail = _deliver()

    assert ok and detail == "slack_dm → U1"
    assert [name for name, _ in sent.calls] == ["slack"]


def test_email_is_the_backup_when_the_preferred_chat_is_not_connected(
    sent: _Sent, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _principal(preferred_channel="slack", email="owner@example.com")  # no Slack id
    _with_gateway(monkeypatch, sent)

    ok, detail = _deliver()

    assert ok and detail == "email → owner@example.com"
    assert [name for name, _ in sent.calls] == ["gmail"]


def test_email_is_the_backup_when_every_chat_send_fails(
    sent: _Sent, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from openexecutive.orchestrator import schedule_tools

    _principal(preferred_channel="slack", slack_user_id="U1", email="owner@example.com")
    _with_gateway(monkeypatch, sent)

    async def _slack_down(args: dict) -> str:  # type: ignore[type-arg]
        sent.calls.append(("slack", args))
        return json.dumps({"error": "token revoked"})

    monkeypatch.setattr(schedule_tools, "handle_send_slack_dm", _slack_down)

    ok, detail = _deliver()

    assert ok and detail == "email → owner@example.com"
    assert [name for name, _ in sent.calls] == ["slack", "gmail"]


def _deliver_result() -> runner.PrincipalDelivery:
    import asyncio

    return asyncio.run(runner._deliver_to_principal("BRIEF"))


def test_no_owner_is_its_own_reason(sent: _Sent) -> None:
    assert _deliver_result().reason == "no_owner"


def test_nothing_connected_is_its_own_reason(sent: _Sent) -> None:
    _principal(email="owner@example.com")  # no gateway, no chat
    result = _deliver_result()
    assert (result.ok, result.reason) == (False, "no_channel")


def test_a_failed_send_is_its_own_reason(sent: _Sent, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import schedule_tools

    async def _broken(args: dict) -> str:  # type: ignore[type-arg]
        raise RuntimeError("telegram down")

    _principal(telegram_chat_id="42")
    monkeypatch.setattr(schedule_tools, "handle_send_telegram_message", _broken)
    result = _deliver_result()
    assert (result.ok, result.reason, result.channel) == (False, "send_failed", None)


def test_the_email_subject_carries_the_users_date(monkeypatch: pytest.MonkeyPatch) -> None:
    from zoneinfo import ZoneInfo

    from openexecutive.memory import workspace_settings

    monkeypatch.setattr(
        workspace_settings, "get_user_timezone", lambda *_a: ZoneInfo("America/Los_Angeles")
    )
    # 02:00 UTC on Saturday is still Friday evening in California.
    evening = datetime(2026, 9, 26, 2, 0, tzinfo=UTC)
    assert runner._email_subject("End-of-Day Digest", evening) == "End-of-Day Digest — Fri 25 Sep"


def test_email_without_a_gateway_is_not_delivered(sent: _Sent) -> None:
    _principal(preferred_channel="email", email="owner@example.com")

    ok, detail = _deliver()

    assert not ok and "no deliverable channel" in detail
    assert sent.calls == []


def test_client_digest_still_delivers(sent: _Sent, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from openexecutive.clients import rotation

    _principal(preferred_channel="email", email="owner@example.com")
    _with_gateway(monkeypatch, sent)

    async def _rotate(_settings: object) -> dict:  # type: ignore[type-arg]
        return {"ran": True, "rotated": ["acme"], "failed": {}, "digest": "# Across your clients"}

    monkeypatch.setattr(rotation, "run_client_rotation", _rotate)
    monkeypatch.setattr(rotation, "seed_client_rotation", lambda: None)
    action_id = episodic.insert_scheduled_action(
        run_at=datetime.now(UTC).isoformat(), channel="__internal__",
        channel_ref="client_rotation", intent_text="rotate", kind="client_rotation",
    )
    action = episodic.get_scheduled_action(action_id)
    assert action is not None

    asyncio.run(runner._execute_action(action, None))

    ((_, call),) = sent.calls
    assert call["arguments"]["subject"].startswith("Across your clients — ")
    assert "<h1>Across your clients</h1>" in call["arguments"]["body"]


def test_brief_with_no_deliverable_channel_is_still_generated_and_stored(
    sent: _Sent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A web-only principal still gets the brief run and artifact (read on the
    Artifacts page); it is just not delivered, so the window does not move,
    and the next occurrence is chained."""
    import asyncio

    from openexecutive.alerts import review
    from openexecutive.briefing import brief_state, narrative_cache
    from openexecutive.workflows import persistence as wf_persistence
    from openexecutive.workflows.base import WorkflowEvent
    from openexecutive.workflows.morning_brief import MorningBriefInput, MorningBriefWorkflow

    _principal(email="owner@example.com")  # "any", an email, but no gateway
    monkeypatch.setattr(narrative_cache, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(wf_persistence, "DB_PATH", episodic.DB_PATH)
    wf_persistence.initialize_runs_db(episodic.DB_PATH)
    ran: list[str] = []

    class _Brief(MorningBriefWorkflow):
        async def run(self, inputs, store):  # type: ignore[override]
            ran.append("brief")
            yield WorkflowEvent(type="result", data={"brief_fingerprint": "fp", "suppressed": False})
            yield WorkflowEvent(type="artifact", content="BRIEF")

        def input_model(self):  # type: ignore[override]
            return MorningBriefInput

    async def _review(**_kw: object) -> None:
        ran.append("review")

    class _NoStore:
        def __init__(self, **_kw: object) -> None: ...

    monkeypatch.setitem(WORKFLOW_REGISTRY, "morning_brief", _Brief())
    monkeypatch.setattr(review, "run_alert_review", _review)
    monkeypatch.setattr("openexecutive.knowledge.store.ChromaDBStore", _NoStore)
    chained: list[str] = []
    monkeypatch.setattr(
        runner, "_enqueue_next_principal_brief", lambda kind, after: chained.append(kind),
    )
    action_id = episodic.insert_scheduled_action(
        run_at=datetime.now(UTC).isoformat(), channel="__internal__", channel_ref="principal",
        intent_text="brief", kind="principal_brief_morning",
    )
    action = episodic.get_scheduled_action(action_id)
    assert action is not None

    asyncio.run(runner._run_principal_brief(action, datetime.now(UTC)))

    assert ran == ["review", "brief"]
    assert sent.calls == []  # nothing delivered
    (run,) = wf_persistence.list_runs(workflow_name="morning_brief")
    assert run["status"] == "done"
    stored = wf_persistence.get_run(run["run_id"])
    assert stored is not None and stored["artifact"] == "BRIEF"
    # Only a delivered brief advances the window.
    assert brief_state.last_delivered("principal_brief_morning") is None
    # Why it wasn't sent is kept for the Briefing's notice.
    outcome = brief_state.last_delivery_outcome()
    assert outcome is not None and outcome.reason == "no_channel"
    row = episodic.get_scheduled_action(action_id)
    assert row is not None and row.status == "done"
    assert chained == ["principal_brief_morning"]
