"""Chat-proposed playbook changes are drafts a person approves or discards."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import skill_drafts as drafts_route
from openexecutive.knowledge import skill_drafts, skills_index, skills_repo
from openexecutive.knowledge.skill_drafts import SkillDraft, SkillDraftNotFoundError
from openexecutive.knowledge.store import ChromaDBStore


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ChromaDBStore]:
    builtin_root = tmp_path / "builtin_skills"
    company_root = tmp_path / "company_skills"
    builtin_root.mkdir()
    company_root.mkdir()
    for mod in (skills_index, skills_repo):
        monkeypatch.setattr(mod, "BUILTIN_SKILLS_PATH", builtin_root)
        monkeypatch.setattr(mod, "_company_skills_path", lambda: company_root)
    yield ChromaDBStore(persist_directory=str(tmp_path / "chroma"))


def _draft(action: str = "create", name: str = "weekly", body: str = "proposed") -> SkillDraft:
    return SkillDraft(
        action=action,  # type: ignore[arg-type]
        name=name,
        category="general" if action != "delete" else "",
        description="d" if action != "delete" else "",
        when_to_use="w" if action != "delete" else "",
        body=body if action != "delete" else "",
    )


def _live(store: ChromaDBStore, name: str = "weekly", body: str = "live") -> None:
    skills_repo.create_skill(
        name=name, description="d", when_to_use="w", category="general", body=body, store=store
    )


def test_drafts_never_reach_the_library(store: ChromaDBStore) -> None:
    skill_drafts.save_draft(_draft())
    assert [d.name for d in skill_drafts.list_drafts()] == ["weekly"]
    assert skill_drafts.get_draft("weekly").proposed_at
    assert "weekly" not in {s.frontmatter.name for s in skills_repo.list_skills()}
    with pytest.raises(skills_repo.SkillNotFoundError):
        skills_repo.get_skill("weekly")
    # Not a playbook name either: a real create is still free to take it.
    assert not skills_repo.name_taken("weekly")


def test_approve_create_update_delete(store: ChromaDBStore) -> None:
    skill_drafts.save_draft(_draft("create", body="v1"))
    result = skill_drafts.approve_draft("weekly", store=store)
    assert result["action"] == "create"
    assert skills_repo.get_skill("weekly").body.strip() == "v1"
    assert skill_drafts.list_drafts() == []

    skill_drafts.save_draft(_draft("update", body="v2"))
    skill_drafts.approve_draft("weekly", store=store)
    assert skills_repo.get_skill("weekly").body.strip() == "v2"

    skill_drafts.save_draft(_draft("delete"))
    assert skill_drafts.approve_draft("weekly", store=store)["outcome"] == "deleted"
    with pytest.raises(skills_repo.SkillNotFoundError):
        skills_repo.get_skill("weekly")


def test_failed_approval_keeps_the_draft(store: ChromaDBStore) -> None:
    skill_drafts.save_draft(_draft("create"))
    _live(store)  # the name was taken meanwhile
    with pytest.raises(skills_repo.SkillConflictError):
        skill_drafts.approve_draft("weekly", store=store)
    assert [d.name for d in skill_drafts.list_drafts()] == ["weekly"]
    assert skills_repo.get_skill("weekly").body.strip() == "live"


def test_discard_and_bad_files(store: ChromaDBStore) -> None:
    skill_drafts.save_draft(_draft())
    skill_drafts.discard_draft("weekly")
    with pytest.raises(SkillDraftNotFoundError):
        skill_drafts.discard_draft("weekly")
    drafts_dir = skills_repo._company_skills_path() / ".drafts"
    (drafts_dir / "junk.json").write_text("{not json", encoding="utf-8")
    (drafts_dir / "other.json").write_text(
        _draft(name="mismatch").model_dump_json(), encoding="utf-8"
    )
    assert skill_drafts.list_drafts() == []
    with pytest.raises(skills_repo.SkillParseError):
        skill_drafts.get_draft("../escape")


@pytest.fixture()
def client(store: ChromaDBStore) -> Iterator[TestClient]:
    app = FastAPI()
    app.state.store = store
    app.include_router(drafts_route.router)
    yield TestClient(app)


def test_review_api(client: TestClient, store: ChromaDBStore) -> None:
    _live(store, body="live body")
    skill_drafts.save_draft(_draft("update", body="proposed body"))
    skill_drafts.save_draft(_draft("create", name="fresh"))

    drafts = {d["name"]: d for d in client.get("/skill-drafts").json()["drafts"]}
    assert drafts["weekly"]["current"]["body"].strip() == "live body"
    assert drafts["weekly"]["body"] == "proposed body"
    assert drafts["fresh"]["current"] is None

    approved = client.post("/skill-drafts/weekly/approve")
    assert approved.status_code == 200
    assert approved.json()["skill"]["body"].strip() == "proposed body"
    assert client.get("/skill-drafts/weekly").status_code == 404

    assert client.delete("/skill-drafts/fresh").status_code == 204
    assert client.delete("/skill-drafts/fresh").status_code == 404
    assert client.get("/skill-drafts").json()["drafts"] == []


def test_review_api_conflict_is_409_and_keeps_draft(
    client: TestClient, store: ChromaDBStore
) -> None:
    skill_drafts.save_draft(_draft("update"))  # its playbook doesn't exist
    resp = client.post("/skill-drafts/weekly/approve")
    assert resp.status_code == 409
    assert client.get("/skill-drafts/weekly").status_code == 200
