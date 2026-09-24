"""Unit tests for the draft_artifact chat/research tool."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from openexecutive.alerts import store as alerts_store
from openexecutive.orchestrator import artifact_tools
from openexecutive.orchestrator.artifact_tools import (
    handle_draft_artifact,
    handle_get_artifact,
    handle_list_artifacts,
)
from openexecutive.people import store as people_store
from openexecutive.workflows import persistence as wf_persistence


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A shared SQLite DB the alert + people stores both resolve to.

    The handler calls `insert_alert` / `find_principal_person` WITHOUT a
    db_path, so they read each module's module-level DB_PATH — patch both.
    Audit logging is fire-and-forget; stub it so tests don't touch a real
    episodic DB and so we can assert the audit row fired.
    """
    db_path = tmp_path / "episodic.db"
    alerts_store.initialize_db(db_path)
    people_store.initialize_db(db_path)
    monkeypatch.setattr(alerts_store, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    return db_path


@pytest.fixture()
def audit_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def _record(event_type, summary, **kwargs):  # noqa: ANN001
        calls.append({"event_type": event_type, "summary": summary, **kwargs})

    monkeypatch.setattr("openexecutive.audit.log_event", _record)
    return calls


async def test_inserts_review_alert(db: Path, audit_calls: list[dict]) -> None:
    result = json.loads(await handle_draft_artifact({
        "title": "Competitor X just raised a Series B",
        "document": "## Summary\n\nThey raised $40M.\n\n- Implication one\n- Implication two",
        "why_interesting": "Changes our fundraising timeline assumptions.",
        "source_urls": ["https://example.com/news", "  "],
        "severity": "high",
    }))

    assert result["ok"] is True
    alert = alerts_store.get_alert(result["alert_id"], db_path=db)
    assert alert is not None
    assert alert.source == "artifact"
    assert alert.topic_tags == ["artifact"]
    assert alert.headline == "Competitor X just raised a Series B"
    assert "They raised $40M." in alert.body
    assert "### Sources" in alert.body
    assert "https://example.com/news" in alert.body
    assert alert.suggested_action == "Changes our fundraising timeline assumptions."
    assert alert.severity == "high"
    assert alert.status == "unread"
    # The audit row fired with the draft_artifact tool tag.
    assert any(c["event_type"] == "tool_invocation"
               and c["details"]["tool"] == "draft_artifact"
               and c["details"]["ok"] is True
               for c in audit_calls)


async def test_routes_to_principal(db: Path, audit_calls: list[dict]) -> None:
    pid = people_store.upsert_person(
        full_name="Jordan", role="CEO", is_principal=True, db_path=db,
    )
    result = json.loads(await handle_draft_artifact({
        "title": "Memo", "document": "Body", "why_interesting": "Worth a read",
    }))
    alert = alerts_store.get_alert(result["alert_id"], db_path=db)
    assert alert is not None
    assert alert.routed_to_person_id == pid


async def test_no_principal_tolerated(db: Path, audit_calls: list[dict]) -> None:
    result = json.loads(await handle_draft_artifact({
        "title": "Memo", "document": "Body", "why_interesting": "Worth a read",
    }))
    assert result["ok"] is True
    alert = alerts_store.get_alert(result["alert_id"], db_path=db)
    assert alert is not None
    assert alert.routed_to_person_id is None


async def test_invalid_severity_defaults_to_medium(db: Path, audit_calls: list[dict]) -> None:
    result = json.loads(await handle_draft_artifact({
        "title": "Memo", "document": "Body", "why_interesting": "x", "severity": "bogus",
    }))
    alert = alerts_store.get_alert(result["alert_id"], db_path=db)
    assert alert is not None
    assert alert.severity == "medium"


@pytest.mark.parametrize("payload", [
    {"title": "Memo", "document": "  ", "why_interesting": "x"},
    {"title": "  ", "document": "Body", "why_interesting": "x"},
    {"title": "Memo", "document": "Body", "why_interesting": "  "},
])
async def test_rejects_missing_required_fields(
    db: Path, audit_calls: list[dict], payload: dict,
) -> None:
    result = json.loads(await handle_draft_artifact(payload))
    assert "error" in result
    assert alerts_store.list_alerts(db_path=db) == []


async def test_does_not_invoke_triage_pipeline(
    db: Path, audit_calls: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifacts must land verbatim — never through the triage rewriter."""
    triage_calls: list[object] = []
    monkeypatch.setattr(
        "openexecutive.alerts.pipeline.schedule_evaluation",
        lambda event: triage_calls.append(event),
    )
    await handle_draft_artifact({
        "title": "Memo", "document": "Body", "why_interesting": "x",
    })
    assert triage_calls == []


# --------------------------------------------------------------------------- #
# Formats, revisions, read-back and indexing
# --------------------------------------------------------------------------- #



@pytest.fixture()
def runs_db(db: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    wf_persistence.initialize_runs_db(db)
    monkeypatch.setattr(wf_persistence, "DB_PATH", db)
    return db


async def _draft(**fields: object) -> dict:
    payload = {"title": "Memo", "document": "Body", "why_interesting": "x", **fields}
    return json.loads(await handle_draft_artifact(payload))


async def test_default_format_is_markdown_and_returns_link(
    db: Path, audit_calls: list[dict],
) -> None:
    result = await _draft()
    assert result["format"] == "markdown"
    assert result["artifact_id"] == f"alert:{result['alert_id']}"
    assert result["url"] == f"/artifacts/alert:{result['alert_id']}"
    alert = alerts_store.get_alert(result["alert_id"], db_path=db)
    assert alert is not None
    assert alert.artifact_format == "markdown"
    assert alert.body == "Body"


async def test_html_artifact_persists_sanitized(db: Path, audit_calls: list[dict]) -> None:
    result = await _draft(format="html",
                          document="<h1>Report</h1><script>x()</script><p>Numbers.</p>")
    alert = alerts_store.get_alert(result["alert_id"], db_path=db)
    assert alert is not None
    assert alert.artifact_format == "html"
    assert "<script" not in alert.body and "<h1>Report</h1>" in alert.body


async def test_xlsx_artifact_persists_sheet_json(db: Path, audit_calls: list[dict]) -> None:
    result = await _draft(format="xlsx", document="",
                          sheets=[{"name": "S", "columns": ["a"], "rows": [[1], [2]]}])
    alert = alerts_store.get_alert(result["alert_id"], db_path=db)
    assert alert is not None
    assert alert.artifact_format == "xlsx"
    assert json.loads(alert.body)["sheets"][0]["rows"] == [[1], [2]]


async def test_link_artifact_persists_url_and_label(db: Path, audit_calls: list[dict]) -> None:
    result = await _draft(format="link", url="https://notion.example/page",
                          link_label="Notion page", document="Plan")
    assert result["external_url"] == "https://notion.example/page"
    alert = alerts_store.get_alert(result["alert_id"], db_path=db)
    assert alert is not None
    assert alert.artifact_url == "https://notion.example/page"
    assert alert.artifact_link_label == "Notion page"


@pytest.mark.parametrize("fields", [
    {"format": "xlsx"},                                  # no sheets
    {"format": "link", "url": "http://insecure.example"},
    {"format": "pdf"},
])
async def test_invalid_format_input_is_an_error(
    db: Path, audit_calls: list[dict], fields: dict,
) -> None:
    result = await _draft(**fields)
    assert "error" in result
    assert alerts_store.list_alerts(db_path=db) == []


async def test_supersedes_archives_prior_and_links_it(
    db: Path, audit_calls: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    unindexed: list[str] = []

    async def _fake_unindex(artifact_id: str) -> None:
        unindexed.append(artifact_id)

    monkeypatch.setattr(artifact_tools, "unindex_artifact", _fake_unindex)
    first = await _draft(document="v1")
    second = await _draft(document="v2", supersedes=first["artifact_id"])

    assert second["superseded"] == first["artifact_id"]
    old = alerts_store.get_alert(first["alert_id"], db_path=db)
    new = alerts_store.get_alert(second["alert_id"], db_path=db)
    assert old is not None and old.archived_at is not None
    assert new is not None and new.supersedes_id == first["artifact_id"]
    assert new.archived_at is None
    assert unindexed == [first["artifact_id"]]


async def test_supersedes_can_revise_a_workflow_run(
    runs_db: Path, audit_calls: list[dict],
) -> None:
    wf_persistence.create_run("run-9", "board_prep", "Deck", {}, db_path=runs_db)
    wf_persistence.complete_run("run-9", "# Deck\n\nv1", db_path=runs_db)
    result = await _draft(format="docx", document="# Deck\n\nv2", supersedes="run:run-9")
    assert result["superseded"] == "run:run-9"
    run = wf_persistence.get_run("run-9", db_path=runs_db)
    assert run is not None and run["archived_at"] is not None


@pytest.mark.parametrize("bad", ["alert:999", "nope", "alert:abc"])
async def test_supersedes_unknown_is_an_error(
    db: Path, audit_calls: list[dict], bad: str,
) -> None:
    result = await _draft(supersedes=bad)
    assert "error" in result and "supersedes" in result["error"]
    assert alerts_store.list_alerts(db_path=db) == []


async def test_supersedes_refuses_non_artifact_alert(db: Path, audit_calls: list[dict]) -> None:
    other = alerts_store.insert_alert(source="email", external_id="e", severity="low",
                                      headline="Inbound", body="x", db_path=db)
    result = await _draft(supersedes=f"alert:{other}")
    assert "error" in result
    inbound = alerts_store.get_alert(other or 0, db_path=db)
    assert inbound is not None and inbound.archived_at is None


async def test_list_artifacts_filters_and_limits(
    runs_db: Path, audit_calls: list[dict],
) -> None:
    await _draft(title="Pricing teardown", document="Acme vs Beta")
    await _draft(title="Hiring plan", document="Two engineers")
    wf_persistence.create_run("r1", "board_prep", "Q3 board deck", {}, db_path=runs_db)
    wf_persistence.complete_run("r1", "# Deck", db_path=runs_db)

    everything = json.loads(await handle_list_artifacts({}))
    assert everything["count"] == 3
    titles = {a["title"] for a in everything["artifacts"]}
    assert titles == {"Pricing teardown", "Hiring plan", "Q3 board deck"}

    hits = json.loads(await handle_list_artifacts({"query": "acme"}))
    assert [a["title"] for a in hits["artifacts"]] == ["Pricing teardown"]

    one = json.loads(await handle_list_artifacts({"limit": 1}))
    assert one["count"] == 1


async def test_get_artifact_returns_display_content_truncated(
    db: Path, audit_calls: list[dict],
) -> None:
    sheet = await _draft(title="Model", format="xlsx", document="Summary",
                         sheets=[{"name": "S", "columns": ["k", "v"], "rows": [["a", 1]]}])
    got = json.loads(await handle_get_artifact({"id": sheet["artifact_id"]}))
    assert got["format"] == "xlsx"
    assert "| k | v |" in got["content"] and "Summary" in got["content"]
    assert got["rationale"] == "x"
    assert got["truncated"] is False

    long_doc = await _draft(document="word " * 500)
    cut = json.loads(await handle_get_artifact({"id": long_doc["artifact_id"],
                                                "max_chars": 200}))
    assert len(cut["content"]) == 200 and cut["truncated"] is True


async def test_get_artifact_html_returns_text_not_markup(
    db: Path, audit_calls: list[dict],
) -> None:
    page = await _draft(format="html", document="<h1>Hi</h1><p>Body text</p>")
    got = json.loads(await handle_get_artifact({"id": page["artifact_id"]}))
    assert "<" not in got["content"] and "Body text" in got["content"]


@pytest.mark.parametrize("bad", ["alert:12345", "garbage", ""])
async def test_get_artifact_errors(db: Path, bad: str) -> None:
    assert "error" in json.loads(await handle_get_artifact({"id": bad}))


async def test_get_artifact_refuses_non_artifact_alert(db: Path) -> None:
    other = alerts_store.insert_alert(source="email", external_id="e", severity="low",
                                      headline="Private", body="secret", db_path=db)
    got = json.loads(await handle_get_artifact({"id": f"alert:{other}"}))
    assert "error" in got and "secret" not in json.dumps(got)


class _FakeStore:
    def __init__(self) -> None:
        self.added: list[dict] = []
        self.deleted: list[tuple] = []

    def add_documents(self, *, texts, metadatas, ids, collection):  # noqa: ANN001
        self.added.append({"texts": texts, "metadatas": metadatas, "collection": collection})

    def delete_documents(self, collection, where):  # noqa: ANN001
        self.deleted.append((collection, where))


async def test_draft_indexes_artifact_into_research_collection(
    db: Path, audit_calls: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    monkeypatch.setattr(artifact_tools, "_knowledge_store", lambda: store)
    result = await _draft(title="Churn memo", document="Churn is up in SMB.")

    assert len(store.added) == 1
    added = store.added[0]
    assert added["collection"] == "recent_research"
    meta = added["metadatas"][0]
    assert meta["type"] == "artifact"
    assert meta["artifact_id"] == result["artifact_id"]
    assert "Churn is up in SMB." in " ".join(added["texts"])

    await artifact_tools.unindex_artifact(result["artifact_id"])
    assert store.deleted == [("recent_research", {"artifact_id": result["artifact_id"]})]


async def test_indexing_failure_does_not_fail_draft(
    db: Path, audit_calls: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> None:
        raise RuntimeError("chroma down")

    monkeypatch.setattr(artifact_tools, "_knowledge_store", _boom)
    result = await _draft()
    assert result["ok"] is True
    assert alerts_store.get_alert(result["alert_id"], db_path=db) is not None
