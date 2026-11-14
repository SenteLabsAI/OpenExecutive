"""The Executive in the person's own browser (browser/relay.py,
browser/tools.py, api/routes/browser.py): steps go only to the person
whose turn asked, only while their extension is asking, and come back as the
tool's result."""
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

from openexecutive.api.routes import browser as browser_route
from openexecutive.browser import relay as browser_relay
from openexecutive.browser import tools as browser_tools
from openexecutive.memory import episodic
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.people import registry
from openexecutive.people import store as people_store


@pytest.fixture(autouse=True)
def fresh() -> Iterator[None]:
    browser_relay._reset_for_tests()
    browser_tools._NEEDED.clear()
    yield
    browser_relay._reset_for_tests()
    browser_tools._NEEDED.clear()


def _session(**kw: Any) -> SimpleNamespace:
    base = {
        "caller_person_id": 7, "from_web_chat": True, "from_host": False, "from_mcp_client": False,
        "origin_channel": "", "origin_channel_ref": "",
    }
    base.update(kw)
    return SimpleNamespace(**base)


async def _extension(person_id: int, answer: dict[str, Any]) -> dict[str, Any]:
    """Take the next step as the extension would, answer it, and return it."""
    step = None
    while step is None:
        step = await browser_relay.next_step(person_id, wait=1)
    assert browser_relay.finish(person_id, step["id"], answer)
    return step


async def test_a_step_goes_to_the_extension_and_its_answer_comes_back() -> None:
    await browser_relay.next_step(7, wait=0)  # the extension is asking
    run = asyncio.create_task(browser_relay.run(7, "click", {"ref": 3}))
    step = await _extension(7, {"ok": True, "clicked": "Save"})
    assert step["action"] == "click" and step["args"] == {"ref": 3}
    assert await run == {"ok": True, "clicked": "Save"}


async def test_no_extension_asking_means_not_connected() -> None:
    with pytest.raises(browser_relay.NotConnected):
        await browser_relay.run(7, "look", {})


async def test_someone_else_cannot_take_or_answer_a_step() -> None:
    await browser_relay.next_step(7, wait=0)
    await browser_relay.next_step(8, wait=0)
    run = asyncio.create_task(browser_relay.run(7, "look", {}))
    assert await browser_relay.next_step(8, wait=0.05) is None
    step = await browser_relay.next_step(7, wait=1)
    assert step is not None
    assert not browser_relay.finish(8, step["id"], {"ok": True})
    assert browser_relay.finish(7, step["id"], {"ok": True, "url": "https://x.example"})
    assert (await run)["url"] == "https://x.example"


async def test_a_step_nobody_answers_times_out_with_a_plain_error() -> None:
    await browser_relay.next_step(7, wait=0)
    out = await browser_relay.run(7, "look", {}, timeout=0.05)
    assert out["ok"] is False and "didn't answer" in out["error"]
    assert not browser_relay._steps


async def test_an_answer_is_cut_to_size_and_a_failure_always_says_why() -> None:
    await browser_relay.next_step(7, wait=0)
    run = asyncio.create_task(browser_relay.run(7, "look", {}))
    await _extension(7, {"ok": True, "text": "x" * (browser_relay.MAX_RESULT_CHARS + 50)})
    assert len((await run)["text"]) == browser_relay.MAX_RESULT_CHARS
    run = asyncio.create_task(browser_relay.run(7, "click", {"ref": 1}))
    await _extension(7, {"ok": False})
    assert (await run)["error"]


def test_offered_on_web_chat_connected_or_not() -> None:
    # Not connected: still offered, and each tool says how to connect.
    assert browser_tools.offered(_session())
    browser_relay._person(7).seen = __import__("time").monotonic()
    assert browser_tools.offered(_session())
    assert not browser_tools.offered(_session(from_web_chat=False, origin_channel="kiosk", origin_channel_ref="app:abc"))
    assert not browser_tools.offered(_session(from_web_chat=False, origin_channel="slack"))
    assert not browser_tools.offered(_session(from_host=True))
    assert not browser_tools.offered(_session(from_mcp_client=True))
    assert not browser_tools.offered(_session(caller_person_id=None))


def test_arguments_are_checked_before_anything_is_sent() -> None:
    assert browser_tools._args("browser_click", {})[0] is None
    assert browser_tools._args("browser_type", {"ref": 2, "text": "Hi", "submit": True})[0] == {
        "ref": 2, "text": "Hi", "submit": True,
    }
    assert browser_tools._args("browser_navigate", {"url": "javascript:alert(1)"})[0] is None
    assert browser_tools._args("browser_navigate", {"url": "https://example.com"})[0] == {"url": "https://example.com"}
    assert browser_tools._args("browser_key", {"key": "F12"})[0] is None
    assert browser_tools._args("browser_scroll", {})[0] == {"direction": "down"}
    assert browser_tools._args("browser_switch_tab", {"tab_id": True})[0] is None


def test_a_new_window_only_when_asked_for() -> None:
    assert browser_tools._args("browser_open_tab", {"url": "https://example.com"})[0] == {"url": "https://example.com"}
    assert browser_tools._args("browser_open_tab", {"url": "https://example.com", "new_window": True})[0] == {
        "url": "https://example.com", "new_window": True,
    }
    assert browser_tools._args("browser_open_tab", {"url": "https://example.com", "new_window": "yes"})[0] == {
        "url": "https://example.com",
    }
    assert browser_tools._args("browser_show_page", {"path": "/apps", "new_window": True})[0] == {
        "path": "/apps", "new_window": True,
    }
    assert browser_tools._args("browser_show_page", {"path": "/apps"})[0] == {"path": "/apps"}
    for walk in ("/x/../api/backend/x", "/./oauth", "/x/..", "/%2e%2e/api", "/x%2Fapi", "/x%5capi"):
        assert browser_tools._args("browser_show_page", {"path": walk})[0] is None, walk
    assert browser_tools._args("browser_show_page", {"path": "/knowledge?q=a/../b"})[0] == {"path": "/knowledge?q=a/../b"}
    # Navigating stays in their tab.
    assert browser_tools._args("browser_navigate", {"url": "https://example.com", "new_window": True})[0] == {
        "url": "https://example.com",
    }
    for name in ("browser_open_tab", "browser_show_page"):
        assert "new_window" in browser_tools.TOOLS[name]["input_schema"]["properties"]


def test_it_opens_open_executive_pages_by_path_and_points_things_out() -> None:
    assert browser_tools._args("browser_show_page", {"path": " /knowledge?tab=docs "})[0] == {"path": "/knowledge?tab=docs"}
    # A link from open_in_app keeps only its path: it opens on their own site.
    got = browser_tools._args("browser_show_page", {"path": "https://oe.example.com/artifacts/alert:4#top"})[0]
    assert got == {"path": "/artifacts/alert:4#top"}
    for bad in ("/api/account/export", "/oauth/authorize", "/mcp", "//evil.com", "knowledge", "/a b", "/a\\b", 7):
        assert browser_tools._args("browser_show_page", {"path": bad})[0] is None
    assert browser_tools._args("browser_point", {"ref": 3})[0] == {"ref": 3}
    assert browser_tools._args("browser_point", {})[0] is None
    # Both only read: they still run after outside content, like looking.
    assert {"browser_show_page", "browser_point"} <= browser_tools.READS
    assert {"show_page", "point"} <= browser_relay.ACTIONS


async def test_the_tool_runs_in_the_speakers_own_browser() -> None:
    token = current_session.set(_session())
    try:
        handle = browser_tools._handler("browser_type")
        out = json.loads(await handle({"ref": 1, "text": "Northwind"}))
        assert "isn't connected" in out["error"]
        await browser_relay.next_step(7, wait=0)
        run = asyncio.create_task(handle({"ref": 1, "text": "Northwind"}))
        step = await _extension(7, {"ok": True})
        assert step["action"] == "type" and step["args"]["text"] == "Northwind"
        assert json.loads(await run) == {"done": True}
        run = asyncio.create_task(handle({"ref": 2, "text": "x"}))
        await _extension(7, {"ok": False, "error": "That's a password field."})
        assert json.loads(await run) == {"error": "That's a password field."}
    finally:
        current_session.reset(token)


async def test_on_an_email_or_chat_site_a_click_waits_on_the_card_below_hands_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.orchestrator import levels

    held: list[tuple[str, dict[str, Any], str]] = []
    monkeypatch.setattr(browser_tools, "_may_send", lambda: False)
    monkeypatch.setattr(
        levels, "hold",
        lambda tool, tool_input, *, mcp, reason, session=None: (
            held.append((tool, tool_input, reason)) or json.dumps({"status": "waiting_for_approval"})
        ),
    )
    inbox = "https://mail.google.com/mail/u/0/#inbox"
    token = current_session.set(_session())
    try:
        await browser_relay.next_step(7, wait=0)
        run = asyncio.create_task(browser_tools._handler("browser_click")({"ref": 4, "_at": "https://evil.example/"}))
        step = await _extension(
            7, {"ok": False, "sending_site": True, "url": inbox, "element": 'button "Send"', "error": "waits"},
        )
        # The extension is told it may not send, and nothing the model wrote
        # picks the page the card is for.
        assert step["args"] == {"ref": 4, "send_ok": False, "window": "side"}
        assert json.loads(await run) == {"status": "waiting_for_approval"}
        assert held == [("browser_click", {"ref": 4, "_at": inbox, "_element": 'button "Send"', "_side": True}, levels.SEND_SITE)]
    finally:
        current_session.reset(token)


async def test_on_hands_off_a_click_goes_on_an_email_or_chat_site(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_tools, "_may_send", lambda: True)
    token = current_session.set(_session())
    try:
        await browser_relay.next_step(7, wait=0)
        run = asyncio.create_task(browser_tools._handler("browser_type")({"ref": 2, "text": "Thanks"}))
        step = await _extension(7, {"ok": True})
        assert step["args"]["send_ok"] is True and "at" not in step["args"]
        assert json.loads(await run) == {"done": True}
    finally:
        current_session.reset(token)


async def test_an_approved_click_runs_in_the_askers_browser_on_that_page() -> None:
    from openexecutive.orchestrator import levels
    from openexecutive.orchestrator.session import Session

    inbox = "https://mail.google.com/mail/u/0/#inbox"
    # Carried out from the card: an unattended run, no speaker of its own.
    token = current_session.set(Session(unattended=True))
    try:
        await browser_relay.next_step(7, wait=0)
        with levels.approved(7):
            run = asyncio.create_task(
                browser_tools._handler("browser_click")({"ref": 4, "_at": inbox, "_element": 'button "Send"'}),
            )
            step = await _extension(7, {"ok": True})
            assert json.loads(await run) == {"done": True}
        # Only on that page, and only on the element the card named.
        assert step["args"] == {"ref": 4, "send_ok": True, "at": inbox, "element": 'button "Send"'}
        # Outside an approved step there is no one to send it to.
        out = json.loads(await browser_tools._handler("browser_click")({"ref": 4}))
        assert "isn't connected" in out["error"]
    finally:
        current_session.reset(token)


def test_a_held_browser_step_says_what_and_where() -> None:
    from openexecutive.orchestrator import take_the_lead

    at = "https://www.linkedin.com/messaging/thread/1"
    assert take_the_lead.summarize("browser_click", {"ref": 4, "_at": at}) == "Click on linkedin.com"
    assert take_the_lead.summarize("browser_click", {"ref": 4, "_at": at, "_element": 'button "Send"'}) == (
        'Click (button "Send") on linkedin.com'
    )
    assert take_the_lead.summarize("browser_type", {"ref": 1, "text": "See you", "_at": at}) == (
        "Type on linkedin.com: “See you”"
    )
    assert take_the_lead.summarize("browser_key", {"key": "Enter"}) == "Press Enter in your browser"


def test_registered_in_the_browser_group_with_reads_marked() -> None:
    from openexecutive.orchestrator import extensions

    seen: list[Any] = []
    original = extensions.register_tool
    extensions.register_tool = seen.append  # type: ignore[assignment]
    try:
        browser_tools.register()
    finally:
        extensions.register_tool = original  # type: ignore[assignment]
    by_name = {t.name: t for t in seen}
    assert set(by_name) == set(browser_tools.TOOLS)
    assert all(t.group == "browser" for t in seen)
    # Reading a page is outside content; looking and moving around still run
    # after one, acting goes through the usual checks.
    assert by_name["browser_look"].reads_outside and by_name["browser_look"].after_mail_read
    assert not by_name["browser_click"].after_mail_read and not by_name["browser_click"].read_only
    # Every step held below Hands-off can wait on the Approve card.
    assert {n for n, t in by_name.items() if t.card} == browser_tools.CARDS
    # Every answer carries page text (titles, labels), and rows stay private.
    assert all(t.reads_outside and t.private_row for t in seen)


# ---- The routes ----


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    from openexecutive.api.routes import browser as route

    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    people_store.initialize_db()
    episodic.initialize_db()
    registry.invalidate()
    who: dict[str, int | None] = {"id": None}
    monkeypatch.setattr("openexecutive.api.routes.chat._resolve_caller_person_id", lambda _r: who["id"])
    app = FastAPI()
    app.include_router(route.router)
    test_client = TestClient(app)
    test_client.who = who  # type: ignore[attr-defined]
    return test_client


def test_routes_are_for_people_on_the_team_and_their_own_steps(client: TestClient) -> None:
    assert client.get("/browser/steps?wait=0").status_code == 403
    pid = people_store.upsert_person(full_name="Dana Lee", email="dana@example.com", is_principal=True)
    client.who["id"] = pid  # type: ignore[attr-defined]
    assert client.get("/browser/steps?wait=0").json() == {"step": None}
    assert browser_relay.connected(pid)
    assert client.post("/browser/steps/nope", json={"ok": True}).status_code == 404
    assert client.post("/browser/steps/nope", content=b"[1]").status_code == 400
    assert client.post("/browser/steps/nope", content=b"[" * 200_000).status_code == 400
    # Too big is refused before it is all read, by its stated length or as it streams.
    big = b" " * (browser_route.MAX_BODY + 1)
    assert client.post("/browser/steps/nope", content=big).status_code == 413
    assert client.post("/browser/steps/nope", content=iter([big[:1000], big[1000:]])).status_code == 413
    assert client.post("/browser/hand-over/nope", content=b" " * 3000).status_code == 413
    hello = client.get("/browser/hello").json()
    assert hello == {"exec_name": hello["exec_name"], "first_name": "Dana", "protocol": 1}
    assert hello["exec_name"]


def test_status_says_whether_stay_connected_is_ticked(client: TestClient) -> None:
    pid = people_store.upsert_person(full_name="Dana Lee", email="dana@example.com", is_principal=True)
    client.who["id"] = pid  # type: ignore[attr-defined]
    status = client.get("/browser/status").json()
    assert status["connected"] is False and status["stay_connected"] is None
    client.get("/browser/steps?wait=0&stay=0")
    assert client.get("/browser/status").json()["stay_connected"] is False
    client.get("/browser/steps?wait=0&stay=1")
    assert client.get("/browser/status").json()["stay_connected"] is True
    # An older extension doesn't say.
    client.get("/browser/steps?wait=0")
    status = client.get("/browser/status").json()
    assert status["connected"] is True and status["stay_connected"] is None





def test_a_deeply_nested_answer_is_cut_not_an_error() -> None:
    deep: Any = "x"
    for _ in range(900):
        deep = [deep]
    clipped = browser_relay._clip({"ok": True, "deep": deep})
    level = clipped["deep"]
    for _ in range(7):
        level = level[0]
    assert level == "…"


# ---- Carried out from the card ----


async def test_a_card_tool_runs_from_its_card_only_when_held_and_in_the_askers_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``take_the_lead.carry_out``: an extension tool marked ``card`` runs
    only from a held step, a tool not marked ``card`` never does, and the
    step goes to the browser of the person who asked, not the approver's."""
    from openexecutive.orchestrator import extensions, take_the_lead

    seen: list[Any] = []
    original = extensions.register_tool
    extensions.register_tool = seen.append  # type: ignore[assignment]
    try:
        browser_tools.register()
    finally:
        extensions.register_tool = original  # type: ignore[assignment]
    by_name = {t.name: t for t in seen}
    monkeypatch.setattr(extensions, "_tools", {**extensions._tools, **by_name})
    inbox = "https://mail.google.com/mail/u/0/#inbox"
    payload = {
        "tool": "browser_click", "input": {"ref": 4, "_at": inbox}, "mcp": False,
        "requested_by_person_id": 7,
    }
    # Not held: nothing runs.
    with pytest.raises(RuntimeError, match="can't be done from here"):
        await take_the_lead.carry_out(payload, by_principal=True)
    # A tool not marked card never runs from a card.
    with pytest.raises(RuntimeError, match="can't be done from here"):
        await take_the_lead.carry_out({**payload, "tool": "browser_look", "held": True}, by_principal=True)
    # Held: it runs in the asker's (7) browser, not the owner's (1).
    await browser_relay.next_step(7, wait=0)
    await browser_relay.next_step(1, wait=0)
    run = asyncio.create_task(take_the_lead.carry_out({**payload, "held": True}, by_principal=True))
    step = await _extension(7, {"ok": True})
    assert json.loads(await run) == {"done": True}
    assert step["action"] == "click" and step["args"]["send_ok"] is True and step["args"]["at"] == inbox


async def test_the_askers_own_tap_carries_out_their_browser_step_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A teammate approving a step held on their own card: for a ``card``
    tool (it only ever runs in their own browser) their tap is the OK, as
    the owner's is; for anything else it relaxes nothing."""
    from openexecutive.orchestrator import extensions, levels, take_the_lead

    seen: list[Any] = []
    original = extensions.register_tool
    extensions.register_tool = seen.append  # type: ignore[assignment]
    try:
        browser_tools.register()
    finally:
        extensions.register_tool = original  # type: ignore[assignment]
    monkeypatch.setattr(extensions, "_tools", {**extensions._tools, **{t.name: t for t in seen}})
    inbox = "https://mail.google.com/mail/u/0/#inbox"
    payload = {
        "tool": "browser_click", "input": {"ref": 4, "_at": inbox}, "mcp": False,
        "requested_by_person_id": 7, "held": True,
    }
    await browser_relay.next_step(7, wait=0)
    run = asyncio.create_task(take_the_lead.carry_out(payload, by_principal=False, own_card=True))
    step = await _extension(7, {"ok": True})
    assert json.loads(await run) == {"done": True}
    assert step["args"]["send_ok"] is True and step["args"]["at"] == inbox
    # Their tap on another kind of card relaxes nothing.
    relaxed: list[bool] = []

    async def skill(_: dict[str, Any]) -> str:
        relaxed.append(levels.is_approved())
        return "{}"

    from openexecutive.orchestrator import executive

    monkeypatch.setitem(executive._ALL_SKILL_HANDLERS, "some_skill", skill)
    await take_the_lead.carry_out({**payload, "tool": "some_skill"}, by_principal=False, own_card=True)
    assert relaxed == [False]


def test_the_browser_is_built_in_and_turns_off_here(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import extensions

    extensions._reset_for_tests()
    try:
        extensions.load()
        assert set(browser_tools.TOOLS) <= set(extensions._tools)
        assert extensions.card_handler("browser_click") is not None
        assert extensions.card_handler("browser_look") is None
        assert extensions.asks_through_approvals("oe__browser_type")
        monkeypatch.setenv(browser_relay.ENV_OFF, "1")
        assert not browser_tools.offered(_session())
    finally:
        extensions._reset_for_tests()


# A tiny valid JPEG start, base64 (the check reads only the first bytes).
_JPEG = "/9j/4AAQSkZJRgABAQAAAQABAAD" + "A" * 201


async def test_a_look_brings_a_screenshot_the_model_sees_beside_the_text() -> None:
    from openexecutive.orchestrator import tool_images

    tool_images._reset_for_tests()
    token = current_session.set(_session())
    try:
        handle = browser_tools._handler("browser_look")
        await browser_relay.next_step(7, wait=0)
        run = asyncio.create_task(handle({}))
        shot = {"media_type": "image/jpeg", "data": _JPEG, "width": 1280, "height": 720}
        await _extension(7, {"ok": True, "url": "https://shop.example/", "text": "Cat trees", "screenshot": shot})
        raw = await run
    finally:
        current_session.reset(token)
    # The picture isn't in the text the model reads, and isn't cut to size.
    assert _JPEG not in raw
    text, image = tool_images.take(raw)
    assert json.loads(text) == {"url": "https://shop.example/", "text": "Cat trees", "done": True}
    assert image is not None and image["data"] == _JPEG and "1280x720" in image["note"]
    # Shown to a model that sees pictures, left off for one that doesn't.
    blocks = tool_images.content(text, image, sees=True)
    assert blocks[0] == {"type": "text", "text": text}
    assert blocks[-1]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": _JPEG}
    assert tool_images.content(text, image, sees=False) == text
    # Taken once: the same marker again brings no picture.
    assert tool_images.take(raw) == (text, None)


def test_a_bad_screenshot_is_dropped_and_the_look_still_answers() -> None:
    from openexecutive.orchestrator import tool_images

    assert tool_images.valid("image/jpeg", _JPEG)
    assert not tool_images.valid("image/gif", _JPEG)
    assert not tool_images.valid("image/jpeg", "not base64 !!")
    assert not tool_images.valid("image/png", _JPEG)
    assert not tool_images.valid("image/jpeg", "A" * (tool_images.MAX_DATA_CHARS + 1))
    # A notice before the marker (the content check's) still finds it.
    marked = tool_images.attach("{}", "image/jpeg", _JPEG, "note")
    assert tool_images.take("NOTICE " + marked)[0] == "NOTICE {}"


async def test_the_relay_keeps_a_screenshot_whole_and_drops_a_bad_one() -> None:
    await browser_relay.next_step(7, wait=0)
    big = "/9j/" + "A" * (browser_relay.MAX_RESULT_CHARS + 100)
    run = asyncio.create_task(browser_relay.run(7, "look", {}))
    await _extension(7, {"ok": True, "screenshot": {"media_type": "image/jpeg", "data": big, "width": 10, "height": 10}})
    assert (await run)["screenshot"]["data"] == big
    run = asyncio.create_task(browser_relay.run(7, "look", {}))
    await _extension(7, {"ok": True, "screenshot": {"media_type": "text/html", "data": "x", "width": 10, "height": 10}})
    assert "screenshot" not in await run


async def test_a_click_on_the_screenshot_goes_by_its_pixels() -> None:
    assert browser_tools._args("browser_click_at", {"x": 320, "y": 220}) == ({"x": 320, "y": 220}, "")
    assert browser_tools._args("browser_click_at", {"x": -1, "y": 2})[0] is None
    assert browser_tools._args("browser_click_at", {"x": True, "y": 2})[0] is None
    assert "browser_click_at" in browser_tools.ACTS
    token = current_session.set(_session())
    try:
        handle = browser_tools._handler("browser_click_at")
        await browser_relay.next_step(7, wait=0)
        run = asyncio.create_task(handle({"x": 320, "y": 220}))
        step = await _extension(7, {"ok": True, "clicked": "Cat tree photo"})
        assert step["action"] == "click_at" and step["args"]["x"] == 320 and step["args"]["send_ok"] is False
        assert json.loads(await run) == {"clicked": "Cat tree photo", "done": True}
    finally:
        current_session.reset(token)


def test_only_models_that_see_pictures_get_them(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.providers import openrouter_catalog
    from openexecutive.providers import registry as providers

    openrouter_catalog._reset_for_tests()
    assert providers.sees_images("claude-sonnet-5-5")
    assert providers.sees_images("openai/gpt-5")
    assert not providers.sees_images("meta-llama/llama-text-only")
    monkeypatch.setattr(openrouter_catalog, "_loaded_models", [])
    monkeypatch.setattr(openrouter_catalog, "_known_ids", frozenset({"google/gemini-x", "openai/o-text"}))
    monkeypatch.setattr(openrouter_catalog, "_loaded_image_input", frozenset({"google/gemini-x"}))
    assert providers.sees_images("google/gemini-x")
    assert not providers.sees_images("openai/o-text")


def test_other_models_get_the_picture_after_the_tool_message() -> None:
    from openexecutive.providers.translator import _user_content_to_openai

    content = [{"type": "tool_result", "tool_use_id": "t1", "content": [
        {"type": "text", "text": "{}"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}},
    ]}]
    msgs = _user_content_to_openai(content)
    assert msgs[0] == {"role": "tool", "tool_call_id": "t1", "content": "{}"}
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"][-1] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}


def test_the_check_reads_the_screenshot_too() -> None:
    """Text in a picture is outside content: the check gets the image block
    the model would see, not only the page's text."""
    from unittest.mock import AsyncMock, patch

    from openexecutive.orchestrator import content_scan, tool_images
    from openexecutive.orchestrator import outside_content as oc
    from openexecutive.orchestrator.session import Session

    image = {"media_type": "image/jpeg", "data": _JPEG, "note": "Screenshot."}
    shown = tool_images.content('{"ok": true}', image, sees=True)

    async def go() -> Any:
        session = Session()
        session.session_id = "s1"
        session.from_web_chat = True
        state = oc.begin_turn(session, user_message="add it to my cart")
        try:
            verdict = SimpleNamespace(flagged=False, unchecked=False, categories=())
            with patch("openexecutive.orchestrator.content_scan.scan", AsyncMock(return_value=verdict)) as scan:
                await oc.mark_read(oc.WEB, {}, shown)
                return scan.await_args.args[0]
        finally:
            oc.end_turn(state)

    checked = asyncio.run(go())
    assert any(b.get("type") == "image" for b in content_scan._turn(checked))


def test_a_dropped_picture_leaves_no_marker() -> None:
    from openexecutive.orchestrator import tool_images

    tool_images._reset_for_tests()
    first = tool_images.attach("first", "image/jpeg", _JPEG, "note")
    for _ in range(tool_images._KEEP):
        tool_images.attach("more", "image/jpeg", _JPEG, "note")
    assert tool_images.take(first) == ("first", None)
    tool_images._reset_for_tests()


def test_only_the_newest_screenshot_stays_in_the_turn() -> None:
    from openexecutive.orchestrator import tool_images

    image = {"media_type": "image/jpeg", "data": _JPEG, "note": "Screenshot."}
    old = {"type": "tool_result", "tool_use_id": "a", "content": tool_images.content("{}", image, sees=True)}
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "add it to my cart"},
        {"role": "user", "content": [old]},
    ]
    tool_images.drop_older(messages)
    blocks = messages[1]["content"][0]["content"]
    assert not any(b["type"] == "image" for b in blocks)
    assert blocks[-1] == {"type": "text", "text": tool_images.DROPPED}
    assert messages[0]["content"] == "add it to my cart"


def test_every_step_that_can_be_held_waits_on_a_card() -> None:
    """Approvals is the one gate: a held step is an Approve card, never a refusal."""
    assert set(browser_tools.TOOLS) == browser_tools.READS | browser_tools.CARDS
    assert not browser_tools.READS & browser_tools.CARDS


def test_a_busy_page_is_cut_to_what_the_check_reads_whole() -> None:
    from openexecutive.orchestrator import content_scan

    page = {
        "url": "https://shop.example/s?k=cat",
        "title": "Results",
        "text": "Cat tree\n" * 3000,
        "elements": [{"ref": i, "role": "link", "name": f"Listing {i} " + "x" * 200} for i in range(400)],
    }
    out = browser_tools._fit(page)
    assert len(json.dumps(out)) <= browser_tools.FIT_CHARS < content_scan.MAX_CHARS
    assert out["elements"][0]["ref"] == 0 and len(out["elements"]) < 400
    assert "left out" in out["left_out"]
    small = {"url": "https://shop.example/", "text": "hi", "elements": []}
    assert browser_tools._fit(small) is small


# ---- Getting the extension ----


async def test_a_turn_that_needed_the_browser_gets_the_add_card_once() -> None:
    session = _session(session_id="chat-1")
    token = current_session.set(session)
    try:
        out = json.loads(await browser_tools._handler("browser_look")({}))
        # The steps are on the card, not in the reply.
        assert "card under your reply" in out["error"] and "Settings" not in out["error"]
    finally:
        current_session.reset(token)
    assert browser_tools.take_needed(session)
    assert not browser_tools.take_needed(session)
    assert not browser_tools.take_needed(_session(session_id="chat-2"))
    assert not browser_tools.take_needed(SimpleNamespace())


async def test_a_connected_browser_brings_no_card() -> None:
    session = _session(session_id="chat-1")
    await browser_relay.next_step(7, wait=0)
    token = current_session.set(session)
    try:
        run = asyncio.create_task(browser_tools._handler("browser_look")({}))
        await _extension(7, {"ok": True, "url": "https://example.com", "title": "Example"})
        assert json.loads(await run)["done"] is True
    finally:
        current_session.reset(token)
    assert not browser_tools.take_needed(session)





# ---- From a chat, or another kind of turn an install adds ----


def test_an_install_can_add_a_kind_of_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    def call(session: Any) -> bool:
        return str(getattr(session, "origin_channel_ref", "") or "").startswith("app:")

    def broken(session: Any) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(browser_tools, "SURFACES", [broken, call])
    assert browser_tools.offered(_session(from_web_chat=False, origin_channel="kiosk", origin_channel_ref="app:abc"))
    assert not browser_tools.offered(_session(from_web_chat=False, origin_channel="kiosk", origin_channel_ref="bot:1"))
    # Never on a host page's turn or another AI app's, whatever an install adds.
    assert not browser_tools.offered(_session(from_host=True, origin_channel_ref="app:abc"))
    assert not browser_tools.offered(_session(from_mcp_client=True, origin_channel_ref="app:abc"))


async def test_a_chat_works_beside_the_chat_and_another_turn_in_their_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_tools, "SURFACES", [lambda s: getattr(s, "origin_channel", "") == "kiosk"])
    token = current_session.set(_session(session_id="chat-1"))
    try:
        await browser_relay.next_step(7, wait=0)
        run = asyncio.create_task(browser_tools._handler("browser_open_tab")({"url": "https://example.com/"}))
        step = await _extension(7, {"ok": True, "url": "https://example.com/", "title": "Example"})
        assert step["args"]["window"] == "side"
        await run
    finally:
        current_session.reset(token)
    token = current_session.set(
        _session(session_id="call-1", from_web_chat=False, origin_channel="kiosk", origin_channel_ref="app:abc")
    )
    try:
        run = asyncio.create_task(browser_tools._handler("browser_open_tab")({"url": "https://example.com/"}))
        step = await _extension(7, {"ok": True, "url": "https://example.com/", "title": "Example"})
        assert "window" not in step["args"]
        await run
    finally:
        current_session.reset(token)


async def test_an_unattended_run_never_rearranges_their_windows() -> None:
    # A scheduled run or a card approved later has no chat on screen to keep.
    token = current_session.set(_session(session_id="sched-1", unattended=True))
    try:
        await browser_relay.next_step(7, wait=0)
        run = asyncio.create_task(browser_tools._handler("browser_open_tab")({"url": "https://example.com/"}))
        step = await _extension(7, {"ok": True, "url": "https://example.com/", "title": "Example"})
        assert "window" not in step["args"]
        await run
    finally:
        current_session.reset(token)


# ---- Away from the computer (another device asked) ----


@pytest.fixture
def away(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_tools, "AWAY", [lambda s: getattr(s, "elsewhere", False) is True])


@pytest.mark.usefixtures("away")
async def test_an_ask_from_away_works_in_the_extensions_own_window_and_brings_a_screenshot() -> None:
    token = current_session.set(_session(session_id="chat-1", elsewhere=True))
    try:
        await browser_relay.next_step(7, wait=0)
        run = asyncio.create_task(browser_tools._handler("browser_look")({}))
        step = await _extension(7, {"ok": True, "url": "https://example.com/", "title": "Example"})
        assert step["args"] == {"window": "own", "shot": True}
        await run
        run = asyncio.create_task(browser_tools._handler("browser_click")({"ref": 3}))
        step = await _extension(7, {"ok": True})
        assert step["args"]["window"] == "own" and "shot" not in step["args"]
        await run
        # Its own pages open where they are, not in the computer's browser.
        out = json.loads(await browser_tools._handler("browser_show_page")({"path": "/knowledge"}))
        assert out["error"] == browser_tools.AWAY_NO_SHOW_PAGE
    finally:
        current_session.reset(token)


@pytest.mark.usefixtures("away")
async def test_away_a_browser_ask_on_the_computer_says_stay_connected() -> None:
    session = _session(session_id="chat-1", elsewhere=True)
    token = current_session.set(session)
    try:
        out = json.loads(await browser_tools._handler("browser_look")({}))
    finally:
        current_session.reset(token)
    assert out["error"] == browser_tools.NOT_CONNECTED_AWAY and "Stay connected" in out["error"]
    assert browser_tools.take_needed(session)


@pytest.mark.usefixtures("away")
async def test_a_step_held_while_away_runs_in_the_same_window_when_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.orchestrator import levels
    from openexecutive.orchestrator.session import Session

    held: list[dict[str, Any]] = []
    monkeypatch.setattr(browser_tools, "_may_send", lambda: False)
    monkeypatch.setattr(
        levels, "hold",
        lambda tool, tool_input, *, mcp, reason, session=None: (
            held.append(tool_input) or json.dumps({"status": "waiting_for_approval"})
        ),
    )
    inbox = "https://mail.google.com/mail/u/0/#inbox"
    token = current_session.set(_session(elsewhere=True))
    try:
        await browser_relay.next_step(7, wait=0)
        run = asyncio.create_task(browser_tools._handler("browser_click")({"ref": 4}))
        await _extension(7, {"ok": False, "sending_site": True, "url": inbox, "element": 'button "Send"'})
        await run
    finally:
        current_session.reset(token)
    assert held == [{"ref": 4, "_at": inbox, "_element": 'button "Send"', "_own": True}]
    token = current_session.set(Session(unattended=True))
    try:
        with levels.approved(7):
            run = asyncio.create_task(browser_tools._handler("browser_click")(held[0]))
            step = await _extension(7, {"ok": True})
            await run
        assert step["args"]["window"] == "own" and step["args"]["at"] == inbox
        # A model's own `_own` outside an approved step changes nothing.
        token2 = current_session.set(_session())
        try:
            run = asyncio.create_task(browser_tools._handler("browser_click")({"ref": 1, "_own": True}))
            step = await _extension(7, {"ok": True})
            await run
            assert step["args"]["window"] == "side"
        finally:
            current_session.reset(token2)
    finally:
        current_session.reset(token)


async def test_a_step_held_in_a_chat_runs_in_the_side_window_when_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.orchestrator import levels
    from openexecutive.orchestrator.session import Session

    held: list[dict[str, Any]] = []
    monkeypatch.setattr(browser_tools, "_may_send", lambda: False)
    monkeypatch.setattr(
        levels, "hold",
        lambda tool, tool_input, *, mcp, reason, session=None: (
            held.append(tool_input) or json.dumps({"status": "waiting_for_approval"})
        ),
    )
    inbox = "https://mail.google.com/mail/u/0/#inbox"
    token = current_session.set(_session())
    try:
        await browser_relay.next_step(7, wait=0)
        run = asyncio.create_task(browser_tools._handler("browser_click")({"ref": 4}))
        await _extension(7, {"ok": False, "sending_site": True, "url": inbox, "element": 'button "Send"'})
        await run
    finally:
        current_session.reset(token)
    assert held == [{"ref": 4, "_at": inbox, "_element": 'button "Send"', "_side": True}]
    token = current_session.set(Session(unattended=True))
    try:
        with levels.approved(7):
            run = asyncio.create_task(browser_tools._handler("browser_click")(held[0]))
            step = await _extension(7, {"ok": True})
            await run
        assert step["args"]["window"] == "side" and step["args"]["at"] == inbox
    finally:
        current_session.reset(token)
    # Outside an approved step a model's own `_side` changes nothing.
    token = current_session.set(_session(unattended=True))
    try:
        url = {"url": "https://example.com/", "_side": True}
        run = asyncio.create_task(browser_tools._handler("browser_open_tab")(url))
        step = await _extension(7, {"ok": True, "url": "https://example.com/", "title": "Example"})
        await run
        assert "window" not in step["args"]
    finally:
        current_session.reset(token)


# ---- Turned off here (OE_BROWSER_OFF) ----


async def test_switched_off_nothing_is_offered_and_a_tool_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(browser_relay.ENV_OFF, "1")
    assert browser_relay.on() is False
    assert not browser_tools.offered(_session())
    token = current_session.set(_session())
    try:
        out = json.loads(await browser_tools._handler("browser_look")({}))
        assert out == {"error": browser_tools.OFF}
    finally:
        current_session.reset(token)
    monkeypatch.delenv(browser_relay.ENV_OFF)
    assert browser_tools.offered(_session())


def test_switched_off_the_routes_refuse_and_status_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid = people_store.upsert_person(full_name="Dana Lee", email="dana@example.com", is_principal=True)
    client.who["id"] = pid  # type: ignore[attr-defined]
    assert client.get("/browser/status").json()["available"] is True
    monkeypatch.setenv(browser_relay.ENV_OFF, "1")
    assert client.get("/browser/status").json() == {
        "available": False, "connected": False, "stay_connected": None, "store_url": None, "protocol": 1,
    }
    assert client.get("/browser/steps?wait=0").status_code == 404
    assert client.get("/browser/hello").status_code == 404
    assert client.post("/browser/steps/nope", json={"ok": True}).status_code == 404
    assert not browser_relay.connected(pid)


def test_the_add_button_goes_to_an_https_page_or_nowhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_tools, "STORE_URL", None)
    monkeypatch.delenv(browser_tools.STORE_URL_ENV, raising=False)
    assert browser_tools.store_url() is None
    page = "https://chromewebstore.google.com/detail/open-executive/abcdefghijklmnopabcdefghijklmnop"
    monkeypatch.setattr(browser_tools, "STORE_URL", page)
    assert browser_tools.store_url() == page
    # An install's own build, hosted wherever it likes.
    monkeypatch.setenv(browser_tools.STORE_URL_ENV, "https://downloads.example.com/oe-extension.zip")
    assert browser_tools.store_url() == "https://downloads.example.com/oe-extension.zip"
    # Set empty: no button (Settings shows how to load it instead).
    monkeypatch.setenv(browser_tools.STORE_URL_ENV, "")
    assert browser_tools.store_url() is None
    for bad in ("http://chromewebstore.google.com/detail/x", "javascript:alert(1)", 'https://x.example/"><b>'):
        monkeypatch.setenv(browser_tools.STORE_URL_ENV, bad)
        assert browser_tools.store_url() is None


def test_the_published_extension_is_offered_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(browser_tools.STORE_URL_ENV, raising=False)
    assert browser_tools.store_url() == browser_tools.STORE_URL


def test_status_says_whether_their_own_browser_is_connected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_tools, "STORE_URL", None)
    monkeypatch.delenv(browser_tools.STORE_URL_ENV, raising=False)
    assert client.get("/browser/status").status_code == 403
    pid = people_store.upsert_person(full_name="Dana Lee", email="dana@example.com", is_principal=True)
    client.who["id"] = pid  # type: ignore[attr-defined]
    assert client.get("/browser/status").json() == {
        "available": True, "connected": False, "stay_connected": None, "store_url": None, "protocol": 1,
    }
    client.get("/browser/steps?wait=0")
    assert client.get("/browser/status").json() == {
        "available": True, "connected": True, "stay_connected": None, "store_url": None, "protocol": 1,
    }
