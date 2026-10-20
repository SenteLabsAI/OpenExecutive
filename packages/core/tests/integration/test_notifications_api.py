"""Integration: a person sets their own notifications over HTTP, and the
scheduler then holds a proactive nudge to them until their quiet hours end."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes.notifications import router
from openexecutive.audit import logger as audit_logger
from openexecutive.memory import episodic
from openexecutive.people import held_messages as held
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.notifications import get_notification_settings
from openexecutive.scheduler.runner import _execute_action

OWNER = {"x-caller-email": "pat@northwind.test"}
SARA = {"x-caller-email": "sara@northwind.test"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    db = tmp_path / "test.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(audit_logger, "_default_logger", audit_logger.AuditLogger(db_path=db))
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    people_registry.invalidate()
    for var in ("BACKEND_SHARED_SECRET", "OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    people_store.upsert_person(full_name="Pat Principal", is_principal=True, email="pat@northwind.test")
    people_store.upsert_person(
        full_name="Sara Kim", email="sara@northwind.test", slack_user_id="U_SARA",
        preferred_channel="slack",
    )
    app = FastAPI()
    app.include_router(router)
    yield TestClient(app)
    people_registry.invalidate()


def _id(name: str) -> int:
    person = next(p for p in people_store.list_people() if p.full_name == name)
    assert person.id is not None
    return person.id


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00", "days": "weekdays"},
        "delivery": "digest",
        "urgent_through": True,
        "timezone": "Europe/London",
    }
    body.update(overrides)
    return body


def test_each_person_reads_and_sets_only_their_own(client: TestClient) -> None:
    mine = client.get("/notifications/settings", headers=SARA)
    assert mine.status_code == 200, mine.text
    got = mine.json()
    assert got["quiet_hours"]["enabled"] is False and got["delivery"] == "instant"
    assert got["urgent_through"] is True and got["gets_briefs"] is False
    assert got["held_count"] == 0

    res = client.put("/notifications/settings", headers=SARA, json=_body())
    assert res.status_code == 200, res.text
    assert res.json()["delivery"] == "digest"
    assert res.json()["effective_timezone"] == "Europe/London"
    assert get_notification_settings(_id("Sara Kim")).quiet_hours.days == "weekdays"
    # The principal's are untouched, and theirs say they get the briefs.
    owner = client.get("/notifications/settings", headers=OWNER).json()
    assert owner["delivery"] == "instant" and owner["gets_briefs"] is True


def test_who_may_call(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    res = client.get("/notifications/settings")
    assert res.status_code == 403 and res.json()["detail"]["code"] == "sign_in_required"
    res = client.get("/notifications/settings", headers={"x-caller-email": "nobody@x.test"})
    assert res.status_code == 403 and res.json()["detail"]["code"] == "not_on_roster"
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    assert client.get("/notifications/settings").json()["gets_briefs"] is True


def test_bad_settings_are_refused(client: TestClient) -> None:
    bad_times = _body(quiet_hours={"enabled": True, "start": "22:00", "end": "22:00", "days": "every_day"})
    assert client.put("/notifications/settings", headers=SARA, json=bad_times).status_code == 422
    assert client.put(
        "/notifications/settings", headers=SARA, json=_body(timezone="Mars/Olympus")
    ).status_code == 422
    assert client.put(
        "/notifications/settings", headers=SARA, json=_body(delivery="hourly")
    ).status_code == 422
    assert client.put(
        "/notifications/settings", headers=SARA, json={**_body(), "person_id": 1}
    ).status_code == 422


def test_back_to_as_they_happen_releases_what_waited(client: TestClient) -> None:
    sara = _id("Sara Kim")
    client.put("/notifications/settings", headers=SARA, json=_body())
    held.hold(sara, "Waiting for the brief", source="reflection", release_at=None)
    assert client.get("/notifications/settings", headers=SARA).json()["held_count"] == 1
    client.put("/notifications/settings", headers=SARA, json=_body(delivery="instant"))
    assert held.for_brief(sara) == []
    assert held.held_count(sara) == 1  # now due, for the next release pass


def test_a_nudge_in_quiet_hours_waits_for_them_to_end(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sara = _id("Sara Kim")
    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).strftime("%H:%M")
    end_at = (now + timedelta(hours=2)).replace(second=0, microsecond=0)
    res = client.put("/notifications/settings", headers=SARA, json=_body(
        quiet_hours={"enabled": True, "start": start, "end": end_at.strftime("%H:%M"), "days": "every_day"},
        delivery="instant", timezone="UTC",
    ))
    assert res.status_code == 200, res.text

    def no_chat(*_a: object, **_k: object) -> None:
        raise AssertionError("the nudge must not be written during quiet hours")

    monkeypatch.setattr("openexecutive.orchestrator.executive.Executive", no_chat)
    action_id = episodic.insert_scheduled_action(
        run_at=(now - timedelta(seconds=1)).isoformat(), channel="slack_dm", channel_ref="U_SARA",
        intent_text="Chase Sara about the Q3 plan", kind="proactive_nudge",
        assigned_to_person_id=sara, scope_key="nudge:initiative:7",
    )
    [claimed] = episodic.claim_due_actions(datetime.now(UTC))
    asyncio.run(_execute_action(claimed, gateway=None))
    stored = episodic.get_scheduled_action(action_id)
    assert stored is not None and stored.status == "pending"
    run_at = datetime.fromisoformat(stored.run_at)
    assert abs((run_at - end_at).total_seconds()) < 60


def test_a_dispatch_whose_message_was_held_is_audited_as_held(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sara = _id("Sara Kim")
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr("openexecutive.audit.log_event", lambda et, summary, **kw: events.append((summary, kw)))

    class _HoldingExec:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        async def chat(self, **_k: object) -> str:
            held.hold(sara, "Nudge text", source="nudge_initiative", release_at=None)
            return "ok"

    class _Profile:
        def is_empty(self) -> bool:
            return True

    monkeypatch.setattr("openexecutive.orchestrator.executive.Executive", _HoldingExec)
    monkeypatch.setattr("openexecutive.onboarding.profile_builder.load_or_create_profile", lambda: _Profile())
    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve", lambda query: "")
    episodic.insert_scheduled_action(
        run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(), channel="slack_dm",
        channel_ref="U_SARA", intent_text="Chase Sara", kind="ad_hoc",
    )
    [claimed] = episodic.claim_due_actions(datetime.now(UTC))
    asyncio.run(_execute_action(claimed, gateway=None))
    [(summary, kw)] = [e for e in events if (e[1].get("details") or {}).get("action_id") == claimed.id]  # type: ignore[union-attr]
    assert kw["details"]["phase"] == "held" and "held" in summary  # type: ignore[index]


def test_turning_quiet_hours_off_releases_what_waited(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    sara = _id("Sara Kim")
    client.put("/notifications/settings", headers=SARA, json=_body(delivery="instant"))
    later = datetime.now(UTC) + timedelta(hours=8)
    held.hold(sara, "Held overnight", source="reflection", release_at=later)
    assert held._due(datetime.now(UTC) + timedelta(minutes=1), None) == {}
    off = {"enabled": False, "start": "22:00", "end": "07:00", "days": "every_day"}
    client.put("/notifications/settings", headers=SARA, json=_body(delivery="instant", quiet_hours=off))
    assert sara in held._due(datetime.now(UTC) + timedelta(minutes=1), None)


def test_moving_time_zone_releases_what_waited(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    sara = _id("Sara Kim")
    client.put("/notifications/settings", headers=SARA, json=_body(delivery="instant"))
    held.hold(sara, "Held overnight", source="reflection", release_at=datetime.now(UTC) + timedelta(hours=8))
    client.put(
        "/notifications/settings", headers=SARA,
        json=_body(delivery="instant", timezone="America/New_York"),
    )
    assert sara in held._due(datetime.now(UTC) + timedelta(minutes=1), None)
