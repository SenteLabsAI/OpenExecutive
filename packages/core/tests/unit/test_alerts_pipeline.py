"""Tests for alert pipeline rules: preferences, mute checks, dispatch dispatch."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openexecutive.alerts import store as alert_store
from openexecutive.alerts.models import (
    AlertChannel,
    AlertEvent,
    AlertSeverity,
    TriageDecision,
    UserPreferences,
)
from openexecutive.alerts.preferences import (
    matches_mute,
    resolve_channels,
    save_preferences,
)
from openexecutive.alerts.store import add_mute, initialize_db, list_alerts


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "alerts.db"
    initialize_db(db_path)
    return db_path


def test_resolve_channels_intersects_with_enabled() -> None:
    prefs = UserPreferences(
        severity_threshold=AlertSeverity.LOW,
        channels_enabled=[AlertChannel.WEB, AlertChannel.PERSISTED],
    )
    out = resolve_channels(
        [AlertChannel.WEB, AlertChannel.SLACK_DM, AlertChannel.EMAIL],
        AlertSeverity.HIGH,
        prefs,
    )
    assert AlertChannel.WEB in out
    assert AlertChannel.SLACK_DM not in out
    assert AlertChannel.PERSISTED in out


def test_resolve_channels_drops_below_threshold() -> None:
    prefs = UserPreferences(severity_threshold=AlertSeverity.HIGH)
    out = resolve_channels(
        [AlertChannel.WEB, AlertChannel.PERSISTED],
        AlertSeverity.MEDIUM,
        prefs,
    )
    assert out == [AlertChannel.PERSISTED]


def test_resolve_channels_quiet_hours_suppresses_non_urgent() -> None:
    prefs = UserPreferences(
        severity_threshold=AlertSeverity.LOW,
        quiet_hours_start="00:00",
        quiet_hours_end="23:59",
        quiet_hours_tz="UTC",
    )
    out = resolve_channels(
        [AlertChannel.WEB, AlertChannel.PERSISTED],
        AlertSeverity.HIGH,
        prefs,
    )
    assert out == [AlertChannel.PERSISTED]


def test_resolve_channels_quiet_hours_passes_urgent_through() -> None:
    prefs = UserPreferences(
        severity_threshold=AlertSeverity.LOW,
        quiet_hours_start="00:00",
        quiet_hours_end="23:59",
        quiet_hours_tz="UTC",
    )
    out = resolve_channels(
        [AlertChannel.WEB, AlertChannel.SLACK_DM, AlertChannel.PERSISTED],
        AlertSeverity.URGENT,
        prefs,
    )
    assert AlertChannel.WEB in out
    assert AlertChannel.SLACK_DM in out


def test_resolve_channels_always_includes_persisted() -> None:
    prefs = UserPreferences(
        severity_threshold=AlertSeverity.LOW,
        channels_enabled=[AlertChannel.WEB],  # PERSISTED missing on purpose
    )
    out = resolve_channels(
        [AlertChannel.WEB],
        AlertSeverity.HIGH,
        prefs,
    )
    assert AlertChannel.PERSISTED in out


def test_matches_mute_substring_case_insensitive() -> None:
    assert matches_mute(["customer", "churn"], ["CHURN"])
    assert matches_mute(["hiring-pipeline"], ["hiring"])
    assert not matches_mute(["finance"], ["legal"])
    assert not matches_mute([], ["anything"])
    assert not matches_mute(["a"], [])


def test_save_and_reload_preferences(db: Path) -> None:
    prefs = UserPreferences(
        severity_threshold=AlertSeverity.HIGH,
        quiet_hours_start="22:00",
        quiet_hours_end="07:00",
        channels_enabled=[AlertChannel.WEB, AlertChannel.PERSISTED],
    )
    save_preferences(prefs, db_path=db)

    from openexecutive.alerts.preferences import get_preferences

    loaded = get_preferences(db_path=db)
    assert loaded.severity_threshold == AlertSeverity.HIGH
    assert loaded.quiet_hours_start == "22:00"
    assert loaded.channels_enabled == [AlertChannel.WEB, AlertChannel.PERSISTED]


def test_evaluate_and_dispatch_persists_alert_and_publishes(monkeypatch, db: Path) -> None:
    """End-to-end pipeline test with TriageAgent stubbed."""
    from openexecutive.agents import triage as triage_module
    from openexecutive.alerts import pipeline, sse_bus

    async def fake_triage(self, event, **kwargs):  # noqa: ARG001 - signature match
        return TriageDecision(
            alert=True,
            severity=AlertSeverity.HIGH,
            channels=[AlertChannel.WEB, AlertChannel.PERSISTED],
            headline="Stub alert",
            body="A real signal",
            suggested_action="Reply within an hour",
            topic_tags=["customer"],
            dedup_key="stub-key",
        )

    monkeypatch.setattr(triage_module.TriageAgent, "triage", fake_triage)

    async def runner() -> tuple:
        queue = sse_bus.subscribe()
        try:
            decision, alert_id = await pipeline.evaluate_and_dispatch(
                AlertEvent(source="email", external_id="msg-x", subject="hi", body="world"),
                db_path=db,
            )
            received: list[dict] = []
            while not queue.empty():
                received.append(queue.get_nowait())
            return decision, alert_id, received
        finally:
            sse_bus.unsubscribe(queue)

    decision, alert_id, received = asyncio.run(runner())

    assert decision.alert is True
    assert alert_id is not None

    persisted = list_alerts(db_path=db)
    assert len(persisted) == 1
    assert persisted[0].headline == "Stub alert"
    assert persisted[0].channels_delivered == ["web", "persisted"]

    assert any(e.get("type") == "alert" for e in received)


def test_evaluate_post_mute_suppresses(monkeypatch, db: Path) -> None:
    """A muted topic produced by the LLM is suppressed after the call."""
    from openexecutive.agents import triage as triage_module
    from openexecutive.alerts import pipeline

    add_mute("hiring", db_path=db)

    async def fake_triage(self, event, **kwargs):  # noqa: ARG001
        return TriageDecision(
            alert=True,
            severity=AlertSeverity.HIGH,
            channels=[AlertChannel.WEB, AlertChannel.PERSISTED],
            headline="New senior eng candidate",
            body="...",
            topic_tags=["hiring"],
            dedup_key="cand-1",
        )

    monkeypatch.setattr(triage_module.TriageAgent, "triage", fake_triage)

    decision, alert_id = asyncio.run(
        pipeline.evaluate_and_dispatch(
            AlertEvent(source="email", external_id="m-mute", subject="cand", body="b"),
            db_path=db,
        )
    )

    assert decision.alert is False
    assert "muted" in decision.reason_if_suppressed
    assert alert_id is None
    assert list_alerts(db_path=db) == []


def test_in_quiet_hours_wraparound_window() -> None:
    """A 22:00 → 07:00 window correctly straddles midnight."""
    from openexecutive.alerts.preferences import _in_quiet_hours

    prefs = UserPreferences(
        quiet_hours_start="22:00",
        quiet_hours_end="07:00",
        quiet_hours_tz="UTC",
    )
    # 23:00 UTC — inside.
    assert _in_quiet_hours(prefs, now=datetime(2030, 1, 1, 23, 0, tzinfo=UTC))
    # 03:00 UTC — inside.
    assert _in_quiet_hours(prefs, now=datetime(2030, 1, 1, 3, 0, tzinfo=UTC))
    # 12:00 UTC — outside.
    assert not _in_quiet_hours(prefs, now=datetime(2030, 1, 1, 12, 0, tzinfo=UTC))


# --------------------------------------------------------------------------- #
# Coalescing, routing, dedup hint (alert lifecycle)
# --------------------------------------------------------------------------- #


def _stub_triage(monkeypatch, *, severity=AlertSeverity.MEDIUM, dedup_key="model-key",
                 tags=None):
    from openexecutive.agents import triage as triage_module

    async def fake_triage(self, event, **kwargs):  # noqa: ARG001
        return TriageDecision(
            alert=True,
            severity=severity,
            channels=[AlertChannel.PERSISTED],
            headline="Stub",
            body=f"body for {event.external_id}",
            topic_tags=list(tags or ["customer"]),
            dedup_key=dedup_key or f"key-{event.external_id}",
        )

    monkeypatch.setattr(triage_module.TriageAgent, "triage", fake_triage)


def _count_dispatches(monkeypatch) -> list:
    from openexecutive.alerts import dispatcher

    calls: list = []

    async def fake_dispatch(alert, channels, **kw):  # noqa: ARG001
        calls.append(alert.id)

    monkeypatch.setattr(dispatcher, "dispatch_all", fake_dispatch)
    return calls


def test_pipeline_routes_person_only_from_the_explicit_field(monkeypatch, db: Path) -> None:
    from openexecutive.alerts import pipeline

    _stub_triage(monkeypatch, dedup_key=None)
    _count_dispatches(monkeypatch)

    async def run():
        _, a = await pipeline.evaluate_and_dispatch(
            AlertEvent(source="email", external_id="f", body="x", routed_to_person_id=4),
            db_path=db,
        )
        _, b = await pipeline.evaluate_and_dispatch(
            AlertEvent(source="email", external_id="u", body="x", user="person:9"),
            db_path=db,
        )
        _, c = await pipeline.evaluate_and_dispatch(
            AlertEvent(source="email", external_id="n", body="x", user="U123"),
            db_path=db,
        )
        return a, b, c

    a, b, c = asyncio.run(run())
    assert alert_store.get_alert(a, db_path=db).routed_to_person_id == 4  # type: ignore[union-attr]
    # `event.user` is a sender-controlled display name on some integrations —
    # a "person:9" there must never pick the routing target.
    assert alert_store.get_alert(b, db_path=db).routed_to_person_id is None  # type: ignore[union-attr]
    assert alert_store.get_alert(c, db_path=db).routed_to_person_id is None  # type: ignore[union-attr]


def test_pipeline_replay_of_same_external_id_stays_a_noop(monkeypatch, db: Path) -> None:
    """A webhook retry (same source + external_id) must not coalesce into a
    second occurrence — it is the same event, not a repeat of the situation."""
    from openexecutive.alerts import pipeline

    _stub_triage(monkeypatch, dedup_key="same-key")
    calls = _count_dispatches(monkeypatch)

    async def run():
        _, a = await pipeline.evaluate_and_dispatch(
            AlertEvent(source="email", external_id="msg-1", body="x"), db_path=db,
        )
        _, b = await pipeline.evaluate_and_dispatch(
            AlertEvent(source="email", external_id="msg-1", body="x"), db_path=db,
        )
        return a, b

    a, b = asyncio.run(run())
    assert a is not None and b is None
    row = alert_store.get_alert(a, db_path=db)
    assert row is not None and row.occurrence_count == 1 and calls == [a]


def test_pipeline_adds_department_tag_from_channel(monkeypatch, db: Path) -> None:
    from openexecutive.alerts import pipeline

    _stub_triage(monkeypatch, tags=["finance"])
    _count_dispatches(monkeypatch)

    async def run():
        _, aid = await pipeline.evaluate_and_dispatch(
            AlertEvent(source="email", external_id="d", body="x", department="Finance"),
            db_path=db,
        )
        return aid

    aid = asyncio.run(run())
    row = alert_store.get_alert(aid, db_path=db)
    assert row is not None and row.topic_tags == ["finance", "department:finance"]


def test_pipeline_strips_collection_tags_from_triage(monkeypatch, db: Path) -> None:
    """Only an extension's own code files into its collection
    (orchestrator/extensions.py): a tag the triage model was talked into
    never reaches the stored alert."""
    from openexecutive.alerts import pipeline

    _stub_triage(monkeypatch, tags=["finance", "collection:widgets"])
    _count_dispatches(monkeypatch)

    async def run():
        _, aid = await pipeline.evaluate_and_dispatch(
            AlertEvent(source="artifact", external_id="c", body="x"),
            db_path=db,
        )
        return aid

    aid = asyncio.run(run())
    row = alert_store.get_alert(aid, db_path=db)
    assert row is not None and row.topic_tags == ["finance"]


def test_pipeline_coalesces_same_key_without_dispatch_and_redispatches_on_escalation(
    monkeypatch, db: Path
) -> None:
    from openexecutive.alerts import pipeline

    calls = _count_dispatches(monkeypatch)

    async def run():
        _stub_triage(monkeypatch, severity=AlertSeverity.LOW, dedup_key="k")
        _, first = await pipeline.evaluate_and_dispatch(
            AlertEvent(source="stock", external_id="day1", body="x"), db_path=db,
        )
        _, second = await pipeline.evaluate_and_dispatch(
            AlertEvent(source="stock", external_id="day2", body="x"), db_path=db,
        )
        _stub_triage(monkeypatch, severity=AlertSeverity.URGENT, dedup_key="k")
        _, third = await pipeline.evaluate_and_dispatch(
            AlertEvent(source="stock", external_id="day3", body="crash"), db_path=db,
        )
        return first, second, third

    first, second, third = asyncio.run(run())
    assert first is not None and second is None and third == first
    row = alert_store.get_alert(first, db_path=db)
    assert row is not None
    assert row.occurrence_count == 3
    assert row.severity == "urgent"
    assert row.body == "body for day3"
    # One dispatch for the insert, none for the quiet repeat, one for the escalation.
    assert calls == [first, first]
    assert len(alert_store.list_alerts(db_path=db)) == 1


def test_pipeline_dedup_hint_overrides_model_key(monkeypatch, db: Path) -> None:
    from openexecutive.alerts import pipeline

    _count_dispatches(monkeypatch)

    async def run():
        _stub_triage(monkeypatch, dedup_key="model-a")
        _, a = await pipeline.evaluate_and_dispatch(
            AlertEvent(source="rss", external_id="1", body="x", dedup_hint="watch:acme-news"),
            db_path=db,
        )
        _stub_triage(monkeypatch, dedup_key="model-b")
        _, b = await pipeline.evaluate_and_dispatch(
            AlertEvent(source="rss", external_id="2", body="x", dedup_hint="watch:acme-news"),
            db_path=db,
        )
        return a, b

    a, b = asyncio.run(run())
    assert a is not None and b is None
    row = alert_store.get_alert(a, db_path=db)
    assert row is not None and row.dedup_key == "watch:acme-news" and row.occurrence_count == 2


def test_triage_text_naming_people_or_figures_not_in_the_event_is_dropped(
    monkeypatch, db: Path,
) -> None:
    """Triage rewrites the event into the text that ships; a colleague or a
    figure the event never mentioned must not reach the stored alert."""
    from openexecutive.agents import triage as triage_module
    from openexecutive.alerts import pipeline
    from openexecutive.briefing import grounding

    monkeypatch.setattr(grounding, "profile_sources", lambda: [])
    monkeypatch.setattr(grounding, "grounding_mode", lambda: "enforce")
    audits: list[dict] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: audits.append({"type": event_type, **kw}),
    )

    async def fake_triage(self, event, **kwargs):  # noqa: ARG001
        return TriageDecision(
            alert=True,
            severity=AlertSeverity.HIGH,
            channels=[AlertChannel.PERSISTED],
            headline="Marcus Lee: lease renewal",
            body="Dana wants to renew 48 units by Friday. Marcus Lee manages the site.",
            suggested_action="Call Marcus Lee",
            dedup_key="lease",
        )

    monkeypatch.setattr(triage_module.TriageAgent, "triage", fake_triage)
    event = AlertEvent(
        source="email", external_id="m-lease", subject="Lease renewal",
        body="Hi — can we renew all 48 units by Friday? Dana", **{"from": "Dana Whitfield"},
    )
    decision, alert_id = asyncio.run(pipeline.evaluate_and_dispatch(event, db_path=db))

    assert alert_id is not None
    [stored] = list_alerts(db_path=db)
    assert stored.headline == "Lease renewal"
    assert stored.body == "Dana wants to renew 48 units by Friday."
    assert stored.suggested_action == ""
    assert decision.body == stored.body
    [row] = [a for a in audits if a["type"] == "grounding"]
    assert "Marcus Lee" in row["details"]["items"]


def test_grounded_triage_text_is_stored_unchanged(monkeypatch, db: Path) -> None:
    from openexecutive.agents import triage as triage_module
    from openexecutive.alerts import pipeline
    from openexecutive.briefing import grounding

    monkeypatch.setattr(grounding, "profile_sources", lambda: [])

    async def fake_triage(self, event, **kwargs):  # noqa: ARG001
        return TriageDecision(
            alert=True, severity=AlertSeverity.HIGH, channels=[AlertChannel.PERSISTED],
            headline="Dana Whitfield: renew 48 units", body="Renewal of 48 units due Friday.",
            suggested_action="Reply to Dana", dedup_key="lease-ok",
        )

    monkeypatch.setattr(triage_module.TriageAgent, "triage", fake_triage)
    event = AlertEvent(
        source="email", external_id="m-ok", subject="Lease renewal",
        body="Can we renew all 48 units by Friday?", **{"from": "Dana Whitfield"},
    )
    asyncio.run(pipeline.evaluate_and_dispatch(event, db_path=db))
    [stored] = list_alerts(db_path=db)
    assert (stored.headline, stored.body, stored.suggested_action) == (
        "Dana Whitfield: renew 48 units", "Renewal of 48 units due Friday.", "Reply to Dana",
    )
