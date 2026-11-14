"""Handing a browser step over to the person (browser/handover.py,
browser_hand_over in browser/tools.py, POST /browser/hand-over):
their browser shows "Waiting on you", they get a Needs you card, and the turn
or workflow run carries on when they press Done, or stops on Stop."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.alerts import store as alert_store
from openexecutive.browser import handover as browser_handover
from openexecutive.browser import relay as browser_relay
from openexecutive.browser import tools as browser_tools
from openexecutive.memory import episodic
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.people import registry
from openexecutive.people import store as people_store


@pytest.fixture(autouse=True)
def fresh() -> Iterator[None]:
    browser_relay._reset_for_tests()
    browser_handover._reset_for_tests()
    browser_tools._NEEDED.clear()
    yield
    browser_relay._reset_for_tests()
    browser_handover._reset_for_tests()
    browser_tools._NEEDED.clear()


@pytest.fixture
def alerts_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "alerts.db"
    monkeypatch.setattr(alert_store, "DB_PATH", path)
    alert_store.initialize_db()
    return path


@pytest.fixture
def chat_turn() -> Iterator[None]:
    token = current_session.set(SimpleNamespace(
        caller_person_id=7, from_web_chat=True, from_host=False, from_mcp_client=False,
        origin_channel="", origin_channel_ref="", unattended=False, session_id="s1",
    ))
    yield
    current_session.reset(token)


async def _take(person_id: int, answer: dict[str, Any]) -> dict[str, Any]:
    step = None
    while step is None:
        step = await browser_relay.next_step(person_id, wait=1)
    assert browser_relay.finish(person_id, step["id"], answer)
    return step


async def _handed(
    tool_input: dict[str, Any], *, sign_in: bool = True
) -> tuple[asyncio.Task[str], dict[str, Any]]:
    """Start the tool, let the extension show the bar, and wait until its
    card is up."""
    await browser_relay.next_step(7, wait=0)
    run = asyncio.create_task(browser_tools._handler("browser_hand_over")(tool_input))
    step = await _take(7, {
        "ok": True, "done": True, "url": "https://shop.example.com/login", "title": "Sign in", "sign_in": sign_in,
    })
    for _ in range(200):
        handover = browser_handover.get(step["args"]["id"])
        if handover is not None and handover.card_id is not None:
            break
        await asyncio.sleep(0.01)
    return run, step


async def test_done_carries_the_turn_on_and_closes_its_card(alerts_db: Path, chat_turn: None) -> None:
    run, step = await _handed({"need": "sign in to Shop", "task": "buy the blue hat"})
    assert step["action"] == "hand_over"
    assert step["args"]["need"] == "sign in to Shop" and step["args"]["window"] == "side"
    handover = browser_handover.get(step["args"]["id"])
    assert handover is not None and handover.card_id is not None
    card = alert_store.get_alert(handover.card_id)
    assert card is not None
    assert card.headline == "Waiting on you: sign in to Shop"
    assert card.needs == "ok" and card.routed_to_person_id == 7
    assert browser_handover.TAG in card.topic_tags and "shop.example.com" in card.body
    assert "buy the blue hat" in card.suggested_action and "buy the blue hat" in card.body

    got, waited = browser_handover.finish(handover.id, 7, "done")
    assert got is handover and waited
    out = json.loads(await run)
    assert out["done"] is True and "Look at the page again" in out["note"]
    assert alert_store.get_alert(handover.card_id).status == "ack"  # type: ignore[union-attr]


async def test_a_done_pressed_before_the_turn_waits_is_not_lost(alerts_db: Path, chat_turn: None) -> None:
    await browser_relay.next_step(7, wait=0)
    run = asyncio.create_task(browser_tools._handler("browser_hand_over")({"need": "sign in to Shop"}))
    step = None
    while step is None:
        step = await browser_relay.next_step(7, wait=1)
    # Already signed in: Done comes while the bar is still being shown.
    got, waited = browser_handover.finish(step["args"]["id"], 7, "done")
    assert got is not None and waited
    assert browser_relay.finish(7, step["id"], {"ok": True, "url": "https://shop.example.com/"})
    out = json.loads(await asyncio.wait_for(run, 5))
    assert out["done"] is True


async def test_stop_tells_the_turn_to_stop(alerts_db: Path, chat_turn: None) -> None:
    run, step = await _handed({"need": "pick the size"})
    handover = browser_handover.get(step["args"]["id"])
    assert handover is not None
    browser_handover.finish(handover.id, 7, "stop")
    out = json.loads(await run)
    assert out["stopped"] is True
    assert alert_store.get_alert(handover.card_id).status == "dismissed"  # type: ignore[arg-type,union-attr]


async def test_dismissing_the_card_stops_the_turn_and_takes_the_bar_down(
    alerts_db: Path, chat_turn: None
) -> None:
    run, step = await _handed({"need": "sign in to Shop"})
    handover = browser_handover.get(step["args"]["id"])
    assert handover is not None and handover.card_id is not None
    # The ack routes are sync: they close it from a worker thread.
    assert await asyncio.to_thread(browser_handover.card_closed, handover.card_id)
    out = json.loads(await run)
    assert out["stopped"] is True
    ended = await _take(7, {"ok": True})
    assert ended["action"] == "hand_over_end" and ended["args"]["id"] == handover.id
    assert browser_handover.get(handover.id) is None
    assert not browser_handover.card_closed(handover.card_id)


async def test_no_done_in_time_ends_the_turn_and_leaves_the_card(
    alerts_db: Path, chat_turn: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_handover, "WAIT_S", 0.05)
    await browser_relay.next_step(7, wait=0)
    run = asyncio.create_task(browser_tools._handler("browser_hand_over")({"need": "enter the code"}))
    step = await _take(7, {"ok": True, "url": "https://shop.example.com/2fa"})
    out = json.loads(await run)
    assert out["waiting"] is True and "press Done" in out["note"]
    handover = browser_handover.get(step["args"]["id"])
    assert handover is not None and handover.future is None
    assert alert_store.get_alert(handover.card_id).status == "unread"  # type: ignore[arg-type,union-attr]
    # Done later: nobody is waiting, so it's for the card's path.
    assert browser_handover.finish(handover.id, 7, "done") == (handover, False)


async def test_only_their_own_hand_over_and_only_once() -> None:
    handover = browser_handover.start(7, "sign in")
    assert browser_handover.finish(handover.id, 8, "done") == (None, False)
    assert browser_handover.finish(handover.id, 7, "maybe") == (None, False)
    assert browser_handover.finish(handover.id, 7, "done") == (handover, False)
    assert browser_handover.finish(handover.id, 7, "done") == (None, False)


def test_a_person_keeps_only_a_few_open() -> None:
    made = [browser_handover.start(7, f"step {i}") for i in range(browser_handover.MAX_PER_PERSON + 2)]
    assert browser_handover.get(made[0].id) is None
    assert browser_handover.get(made[-1].id) is not None
    assert len([h for h in browser_handover._open.values() if h.person_id == 7]) == browser_handover.MAX_PER_PERSON


async def test_not_connected_says_so_and_makes_no_card(alerts_db: Path, chat_turn: None) -> None:
    out = json.loads(await browser_tools._handler("browser_hand_over")({"need": "sign in"}))
    assert "isn't connected" in out["error"]
    assert browser_handover._open == {}


async def test_it_needs_to_say_what(chat_turn: None) -> None:
    out = json.loads(await browser_tools._handler("browser_hand_over")({"need": "  "}))
    assert "Say what they need to do" in out["error"]


async def test_a_workflow_step_pauses_the_run_until_done(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import extensions

    waits: list[tuple[str, str]] = []
    monkeypatch.setattr(extensions, "in_workflow_step", lambda name: True)
    monkeypatch.setattr(extensions, "workflow_wait", lambda key, what: waits.append((key, what)) or "paused")
    monkeypatch.setattr(browser_tools, "_workflow", lambda: ("buy_wf", 7))
    await browser_relay.next_step(7, wait=0)
    run = asyncio.create_task(browser_tools._handler("browser_hand_over")({"need": "sign in to Shop"}))
    step = await _take(7, {"ok": True})
    assert await run == "paused"
    assert step["args"]["window"] == "own"
    handover = browser_handover.get(step["args"]["id"])
    assert handover is not None and handover.card_id is None
    assert waits == [(f"handover:7:{handover.id}", "you to sign in to Shop")]


# ---- The bar's Done and Stop (POST /browser/hand-over/{id}) ----


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    from openexecutive.api.routes import browser as route

    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(alert_store, "DB_PATH", path)
    people_store.initialize_db()
    episodic.initialize_db()
    alert_store.initialize_db()
    registry.invalidate()
    pid = people_store.upsert_person(full_name="Dana Lee", email="dana@example.com", is_principal=True)
    monkeypatch.setattr("openexecutive.api.routes.chat._resolve_caller_person_id", lambda _r: pid)
    app = FastAPI()
    app.include_router(route.router)
    test_client = TestClient(app)
    test_client.pid = pid  # type: ignore[attr-defined]
    return test_client


def test_the_bar_answers_only_done_or_stop_on_an_open_one(client: TestClient) -> None:
    pid = client.pid  # type: ignore[attr-defined]
    assert client.post("/browser/hand-over/nope", json={"outcome": "done"}).status_code == 404
    handover = browser_handover.start(pid, "sign in")
    assert client.post(f"/browser/hand-over/{handover.id}", json={"outcome": "later"}).status_code == 422
    assert client.post(f"/browser/hand-over/{handover.id}", content=b"[").status_code == 400
    # Someone else's is not theirs to finish.
    other = browser_handover.start(pid + 1, "sign in")
    assert client.post(f"/browser/hand-over/{other.id}", json={"outcome": "done"}).status_code == 404


def test_done_on_the_bar_picks_up_a_waiting_workflow_run_and_stop_stops_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.workflows import resumer

    calls: list[tuple[str, str]] = []

    async def resume(key: str, db_path: Any = None) -> int:
        calls.append(("resume", key))
        return 1

    async def stop(key: str, db_path: Any = None) -> int:
        calls.append(("stop", key))
        return 1

    monkeypatch.setattr(resumer, "resume_waiting", resume)
    monkeypatch.setattr(resumer, "stop_waiting", stop)
    pid = client.pid  # type: ignore[attr-defined]
    first = browser_handover.start(pid, "sign in", workflow_key="handover:1:a")
    second = browser_handover.start(pid, "pick", workflow_key="handover:1:b")
    assert client.post(f"/browser/hand-over/{first.id}", json={"outcome": "done"}).json() == {
        "ok": True, "carried_on": True,
    }
    assert client.post(f"/browser/hand-over/{second.id}", json={"outcome": "stop"}).json() == {
        "ok": True, "carried_on": False,
    }
    assert calls == [("resume", "handover:1:a"), ("stop", "handover:1:b")]


def test_after_the_turn_ended_the_bar_never_carries_on_by_itself(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.routes import alerts as alerts_route

    approved: list[int] = []

    async def approve(alert_id: int, body: Any, request: Any) -> None:
        approved.append(alert_id)

    monkeypatch.setattr(alerts_route, "approve_alert", approve)
    pid = client.pid  # type: ignore[attr-defined]
    # The page's own "Done" (a sign-in left) with nobody waiting: kept open.
    auto = browser_handover.start(pid, "sign in", card_id=12)
    auto.sign_in = True
    assert client.post(f"/browser/hand-over/{auto.id}", json={"outcome": "auto"}).json()["kept"] is True
    assert browser_handover.get(auto.id) is auto
    # Their Done on the bar: the bar goes, the card carries it on.
    assert client.post(f"/browser/hand-over/{auto.id}", json={"outcome": "done"}).json() == {
        "ok": True, "carried_on": False, "on_card": True,
    }
    assert approved == []

    card = alert_store.insert_alert(
        source=browser_handover.SOURCE, external_id="x", severity="high", headline="Waiting on you: pick",
        body="b", routed_to_person_id=pid, needs="ok",
    )
    stopped = browser_handover.start(pid, "pick", card_id=card)
    assert client.post(f"/browser/hand-over/{stopped.id}", json={"outcome": "stop"}).json()["carried_on"] is False
    assert alert_store.get_alert(card).status == "dismissed"  # type: ignore[arg-type,union-attr]
    assert approved == []


async def test_the_pages_own_done_answers_a_turn_still_waiting(alerts_db: Path, chat_turn: None) -> None:
    run, step = await _handed({"need": "sign in to Shop"})
    handover = browser_handover.get(step["args"]["id"])
    assert handover is not None and browser_handover.waited_on(handover)
    browser_handover.finish(handover.id, 7, "done")
    assert json.loads(await run)["done"] is True


def test_registered_as_a_read_that_runs_after_a_page_was_read() -> None:
    from openexecutive.orchestrator import extensions

    seen: list[Any] = []
    original = extensions.register_tool
    extensions.register_tool = seen.append  # type: ignore[assignment]
    try:
        browser_tools.register()
    finally:
        extensions.register_tool = original  # type: ignore[assignment]
    tool = next(t for t in seen if t.name == "browser_hand_over")
    assert tool.read_only and tool.after_mail_read and tool.workflow and not tool.card
    assert {"hand_over", "hand_over_end"} <= browser_relay.ACTIONS


def test_done_on_the_bar_looks_again_for_a_run_not_saved_yet(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.routes import browser as route
    from openexecutive.workflows import resumer

    found = iter([0, 0, 1])

    async def resume(key: str, db_path: Any = None) -> int:
        return next(found)

    monkeypatch.setattr(resumer, "resume_waiting", resume)
    monkeypatch.setattr(route, "WORKFLOW_RETRY_S", 0.0)
    handover = browser_handover.start(client.pid, "sign in", workflow_key="handover:1:c")  # type: ignore[attr-defined]
    assert client.post(f"/browser/hand-over/{handover.id}", json={"outcome": "done"}).json()["carried_on"] is True


def test_done_with_no_run_to_carry_on_keeps_the_bar_so_it_can_be_pressed_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.routes import browser as route
    from openexecutive.workflows import resumer

    found = iter([0] * route.WORKFLOW_TRIES + [1])

    async def resume(key: str, db_path: Any = None) -> int:
        return next(found)

    monkeypatch.setattr(resumer, "resume_waiting", resume)
    monkeypatch.setattr(route, "WORKFLOW_RETRY_S", 0.0)
    handover = browser_handover.start(client.pid, "pick a size", workflow_key="handover:1:d")  # type: ignore[attr-defined]
    first = client.post(f"/browser/hand-over/{handover.id}", json={"outcome": "done"}).json()
    assert first == {"ok": True, "carried_on": False, "kept": True}
    assert browser_handover.get(handover.id) is not None
    again = client.post(f"/browser/hand-over/{handover.id}", json={"outcome": "done"}).json()
    assert again == {"ok": True, "carried_on": True}
    assert browser_handover.get(handover.id) is None


async def test_the_extension_says_whether_the_page_was_a_sign_in(alerts_db: Path, chat_turn: None) -> None:
    run, step = await _handed({"need": "sign in to Shop"})
    assert browser_handover.get(step["args"]["id"]).sign_in is True  # type: ignore[union-attr]
    browser_handover.finish(step["args"]["id"], 7, "stop")
    await run
    run, step = await _handed({"need": "pick a size"}, sign_in=False)
    assert browser_handover.get(step["args"]["id"]).sign_in is False  # type: ignore[union-attr]
    browser_handover.finish(step["args"]["id"], 7, "stop")
    await run


def test_the_pages_own_done_is_taken_only_on_a_sign_in_hand_over(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.workflows import resumer

    resumed: list[str] = []

    async def resume(key: str, db_path: Any = None) -> int:
        resumed.append(key)
        return 1

    monkeypatch.setattr(resumer, "resume_waiting", resume)
    pid = client.pid  # type: ignore[attr-defined]
    # Not a sign-in when handed over (a choice of theirs): a page left later
    # can't say Done for them.
    choice = browser_handover.start(pid, "pick a size", workflow_key="handover:1:e")
    out = client.post(f"/browser/hand-over/{choice.id}", json={"outcome": "auto"}).json()
    assert out == {"ok": True, "carried_on": False, "kept": True}
    assert browser_handover.get(choice.id) is choice and resumed == []
    # A sign-in: the run carries on.
    signin = browser_handover.start(pid, "sign in", workflow_key="handover:1:f")
    signin.sign_in = True
    out = client.post(f"/browser/hand-over/{signin.id}", json={"outcome": "auto"}).json()
    assert out == {"ok": True, "carried_on": True}
    assert resumed == ["handover:1:f"]


def test_done_on_an_old_hand_over_with_no_run_takes_the_bar_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.routes import browser as route
    from openexecutive.workflows import resumer

    async def resume(key: str, db_path: Any = None) -> int:
        return 0

    monkeypatch.setattr(resumer, "resume_waiting", resume)
    monkeypatch.setattr(route, "WORKFLOW_RETRY_S", 0.0)
    handover = browser_handover.start(client.pid, "pick a size", workflow_key="handover:1:g")  # type: ignore[attr-defined]
    # Its run was stopped from its card long ago: nothing will wait again.
    handover.created -= browser_handover.FRESH_S
    out = client.post(f"/browser/hand-over/{handover.id}", json={"outcome": "done"}).json()
    assert out == {"ok": True, "carried_on": False}
    assert browser_handover.get(handover.id) is None
