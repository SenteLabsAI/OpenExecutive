"""Routes behind the knowledge page's connected sources (Google Drive, Notion).

The lists and viewers read the sync state files only; "Sync now" starts one
tick of the same sync the scheduler runs, guarded so it can't overlap a
running tick or fire again straight after one.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import documents
from openexecutive.knowledge import drive_sync, notion_sync

from ._fake_store import FakeStore

DRIVE_ID = "1docAAAAAAAA"
PAGE_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def company(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VECTOR_STORE_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(tmp_path / "company" / "profile.yaml"))
    monkeypatch.setenv("DRIVE_SYNC_ENABLED", "true")
    monkeypatch.setenv("DRIVE_SYNC_SERVICE_ACCOUNT_FILE", str(tmp_path / "sa.json"))
    monkeypatch.setenv("DRIVE_SYNC_FOLDER_IDS", "1rootAAAAAAA")
    monkeypatch.setenv("NOTION_SYNC_ENABLED", "false")
    # Fresh locks per test: a module-level asyncio.Lock binds to the first
    # loop that waits on it.
    monkeypatch.setattr(drive_sync, "_RUN_LOCK", asyncio.Lock())
    monkeypatch.setattr(notion_sync, "_RUN_LOCK", asyncio.Lock())
    monkeypatch.setattr(drive_sync, "_last_finished_at", None)
    monkeypatch.setattr(notion_sync, "_last_finished_at", None)
    root = tmp_path / "company"
    (root / "docs" / "drive").mkdir(parents=True)
    (root / "docs" / "notion").mkdir(parents=True)
    return root


@pytest.fixture
def client(company: Path) -> TestClient:
    app = FastAPI()
    app.include_router(documents.router)
    app.state.store = FakeStore()
    return TestClient(app)


def _seed_drive(company: Path, **state: Any) -> None:
    (company / "docs" / "drive" / "drive-1docaaaa-plan.md").write_text(
        f"<!-- drive_file_id: {DRIVE_ID} -->\n\n# Plan\n\nRevenue grows.\n"
    )
    (company / "drive_sync_state.json").write_text(
        json.dumps(
            {
                "last_run": "2026-09-30T10:00:00+00:00",
                "files": {
                    DRIVE_ID: {
                        "name": "Plan",
                        "filename": "drive-1docaaaa-plan.md",
                        "url": f"https://docs.google.com/d/{DRIVE_ID}",
                        "modified": "2026-09-29T08:00:00Z",
                        "synced_at": "2026-09-30T10:00:00+00:00",
                    },
                    "1emptyAAAAAA": {"name": "Scan", "filename": ""},
                },
                **state,
            }
        )
    )


def test_drive_files_list_and_open(client: TestClient, company: Path) -> None:
    _seed_drive(company)
    body = client.get("/documents/drive").json()
    assert body["enabled"] is True and body["last_run"] == "2026-09-30T10:00:00+00:00"
    plan = next(f for f in body["files"] if f["id"] == DRIVE_ID)
    assert plan == {
        "id": DRIVE_ID,
        "name": "Plan",
        "url": f"https://docs.google.com/d/{DRIVE_ID}",
        "modified_at": "2026-09-29T08:00:00Z",
        "synced_at": "2026-09-30T10:00:00+00:00",
        "indexed": True,
    }
    doc = client.get(f"/documents/drive/{DRIVE_ID}").json()
    assert doc["name"] == "Plan" and doc["content"].startswith("# Plan")
    assert client.get("/documents/drive/1emptyAAAAAA").status_code == 404
    assert client.get("/documents/drive/unknownAAAAA").status_code == 404
    assert client.get("/documents/drive/..%2F..%2Fprofile.yaml").status_code == 404


def test_notion_pages_list_and_open(client: TestClient, company: Path) -> None:
    (company / "docs" / "notion" / "notion-11111111-okrs.md").write_text(
        f"<!-- notion_page_id: {PAGE_ID} -->\n\n# OKRs\n\nShip it.\n"
    )
    (company / "notion_sync_state.json").write_text(
        json.dumps(
            {
                "last_run": "2026-09-30T09:00:00+00:00",
                "pages": {
                    PAGE_ID: {
                        "title": "OKRs",
                        "filename": "notion-11111111-okrs.md",
                        "last_edited": "2026-09-29T00:00:00.000Z",
                    }
                },
            }
        )
    )
    body = client.get("/documents/notion").json()
    assert body["enabled"] is False
    assert body["files"][0]["name"] == "OKRs"
    assert body["files"][0]["synced_at"] == "2026-09-30T09:00:00+00:00"  # legacy fallback
    assert "Ship it." in client.get(f"/documents/notion/{PAGE_ID}").json()["content"]


def test_sources_report_status(client: TestClient, company: Path) -> None:
    _seed_drive(company, last_error="Could not sign in to Google Drive.")
    sources = {s["id"]: s for s in client.get("/documents/sources").json()["sources"]}
    assert sources["drive"]["enabled"] is True and sources["drive"]["file_count"] == 1
    assert sources["drive"]["last_error"] == "Could not sign in to Google Drive."
    assert sources["drive"]["syncing"] is False
    assert sources["notion"]["enabled"] is False and sources["notion"]["last_run"] is None


def test_upload_list_carries_source_and_domain(client: TestClient, company: Path) -> None:
    (company / "docs" / "plan.md").write_text("# Plan")
    store: FakeStore = client.app.state.store  # type: ignore[attr-defined]
    store.add_documents(
        texts=["# Plan"],
        metadatas=[{"filename": "plan.md", "domain": "finance"}],
        ids=["c1"],
        collection="company_docs",
    )
    docs = client.get("/documents").json()["documents"]
    assert [(d["filename"], d["source"], d["domain"]) for d in docs] == [
        ("plan.md", "upload", "finance")
    ]


def test_sync_now_starts_one_tick(
    client: TestClient, company: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_run() -> dict[str, int]:
        calls.append("drive")
        return {"updated": 0}

    monkeypatch.setattr(drive_sync, "run_drive_sync", fake_run)
    resp = client.post("/documents/sources/drive/sync")
    assert resp.status_code == 202, resp.text
    # The tick runs as a background task on the client's event loop.
    deadline = time.monotonic() + 2
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == ["drive"]


def test_sync_now_refuses_while_running_disabled_or_just_synced(
    client: TestClient, company: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(drive_sync, "is_syncing", lambda: True)
    assert client.post("/documents/sources/drive/sync").status_code == 409
    monkeypatch.setattr(drive_sync, "is_syncing", lambda: False)

    recent = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    _seed_drive(company, last_run=recent)
    resp = client.post("/documents/sources/drive/sync")
    assert resp.status_code == 429 and int(resp.headers["Retry-After"]) > 0

    assert client.post("/documents/sources/notion/sync").status_code == 409  # not connected
    assert client.post("/documents/sources/dropbox/sync").status_code == 422


def test_sync_now_failure_stays_in_the_background(
    client: TestClient, company: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom() -> dict[str, int]:
        raise RuntimeError("network down")

    monkeypatch.setattr(drive_sync, "run_drive_sync", boom)
    assert client.post("/documents/sources/drive/sync").status_code == 202


def test_a_failed_tick_still_starts_the_cooldown(client: TestClient, company: Path) -> None:
    # A failing source never stamps last_run; the cooldown must still hold,
    # or it could be re-run back-to-back. The service-account file is missing.
    _seed_drive(company, last_run="2026-01-01T00:00:00+00:00")
    asyncio.run(drive_sync.run_drive_sync(store=FakeStore()))  # type: ignore[arg-type]
    assert drive_sync.last_finished_at() is not None
    assert client.post("/documents/sources/drive/sync").status_code == 429


def test_two_sync_now_requests_at_once_start_only_one(
    client: TestClient, company: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first request's task has not taken the sync lock yet when the
    # second arrives, so is_syncing() alone would let both through.
    release = asyncio.Event()

    async def slow_run() -> dict[str, int]:
        await release.wait()
        return {"updated": 0}

    async def both_at_once() -> tuple[int, int]:
        import httpx

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            first, second = await asyncio.gather(
                http.post("/documents/sources/drive/sync"),
                http.post("/documents/sources/drive/sync"),
            )
            assert "drive" in documents._SYNC_TASKS
            release.set()
            await documents._SYNC_TASKS["drive"]
            await asyncio.sleep(0)
        return first.status_code, second.status_code

    monkeypatch.setattr(drive_sync, "run_drive_sync", slow_run)
    monkeypatch.setattr(documents, "_SYNC_TASKS", {})
    assert sorted(asyncio.run(both_at_once())) == [202, 409]
    assert documents._SYNC_TASKS == {}
