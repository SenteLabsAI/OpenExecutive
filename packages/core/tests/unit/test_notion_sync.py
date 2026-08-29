"""Notion → company-docs sync: markdown conversion, watermark skip, ingest."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openexecutive.config import Settings
from openexecutive.knowledge.notion_sync import (
    _page_edited_after,
    block_to_markdown,
    infer_domain,
    page_title,
    rich_text_to_plain,
    run_notion_sync,
    slugify,
)


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "exec@example.com")
    monkeypatch.delenv("NOTION_SYNC_ENABLED", raising=False)
    monkeypatch.delenv("NOTION_API_KEY", raising=False)


def test_rich_text_and_headings() -> None:
    assert rich_text_to_plain([{"plain_text": "Hello"}, {"plain_text": " world"}]) == "Hello world"
    block = {
        "type": "heading_2",
        "heading_2": {"rich_text": [{"plain_text": "Budget"}]},
    }
    assert block_to_markdown(block) == "## Budget"


def test_infer_domain_from_title() -> None:
    assert infer_domain("Q3 finance review") == "finance"
    assert infer_domain("Random wiki page") == "general"


def test_watermark_skip() -> None:
    page = {"last_edited_time": "2026-01-02T00:00:00.000Z"}
    assert _page_edited_after(page, None) is True
    assert _page_edited_after(page, "2026-01-01T00:00:00.000Z") is True
    assert _page_edited_after(page, "2026-01-03T00:00:00.000Z") is False


def test_slugify_is_stable() -> None:
    name = slugify("Comp Bands!", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert name.startswith("notion-aaaaaaaa-")
    assert name.endswith(".md")


def test_page_title_from_properties() -> None:
    page = {
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "Handbook"}]},
        }
    }
    assert page_title(page) == "Handbook"


def test_enabled_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_SYNC_ENABLED", "true")
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    with pytest.raises(ValueError, match="NOTION_API_KEY"):
        Settings(_env_file=None)


@pytest.mark.asyncio
async def test_run_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.knowledge.notion_sync.get_settings",
        lambda: Settings(_env_file=None, ANTHROPIC_API_KEY="k"),
    )
    stats = await run_notion_sync(store=MagicMock())
    assert stats["seen"] == 0
    assert stats["updated"] == 0


def _page(pid: str, edited: str, title: str) -> dict[str, Any]:
    return {
        "object": "page",
        "id": pid,
        "last_edited_time": edited,
        "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}},
    }


@pytest.mark.asyncio
async def test_sync_indexes_new_and_skips_old(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTION_SYNC_ENABLED", "true")
    monkeypatch.setenv("NOTION_API_KEY", "ntn_test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(tmp_path / "profile.yaml"))

    new_page = _page("page-new", "2026-06-02T00:00:00.000Z", "OKRs")
    old_page = _page("page-old", "2026-01-01T00:00:00.000Z", "Archive")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={"results": [new_page, old_page], "has_more": False},
            )
        if "/blocks/" in request.url.path and request.url.path.endswith("/children"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "type": "paragraph",
                            "has_children": False,
                            "paragraph": {"rich_text": [{"plain_text": "Body"}]},
                        }
                    ],
                    "has_more": False,
                },
            )
        return httpx.Response(404, json={"message": request.url.path})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, headers={"Authorization": "Bearer x"})

    state_file = tmp_path / "notion_sync_state.json"
    state_file.write_text(
        '{"watermark": "2026-06-01T00:00:00.000Z", "pages": {}}\n', encoding="utf-8"
    )

    store = MagicMock()
    with (
        patch("openexecutive.knowledge.notion_sync._state_path", return_value=state_file),
        patch("openexecutive.knowledge.notion_sync._docs_dir", return_value=tmp_path / "docs"),
        patch("openexecutive.knowledge.notion_sync._sleep", new=AsyncMock()),
        patch(
            "openexecutive.knowledge.notion_sync.ingest_text",
            new=AsyncMock(return_value=2),
        ) as ingest,
    ):
        (tmp_path / "docs").mkdir()
        stats = await run_notion_sync(store=store, client=client)

    assert stats["seen"] == 2
    assert stats["updated"] == 1
    assert stats["skipped"] == 1
    ingest.assert_awaited_once()
    store.delete_documents.assert_called()


def test_cli_exposes_sync_notion() -> None:
    from openexecutive.cli import cli

    assert "sync-notion" in cli.commands

