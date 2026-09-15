"""HTTP-level tests for conversational onboarding (/onboard/interview/*).

Two properties dominate this file, both carried forward from the wizard:

* **Nothing echoes the user's input.** Transcripts carry ARR, burn, and
  runway, so every rejection path is asserted not to contain the secret token
  the test fed in.
* **A failed commit is retryable.** The wizard needed a deepcopy snapshot for
  this; here the interview never writes, so the assertion is that a rejected
  commit leaves no profile.yaml and an unsaved session.

It also pins the department reconcile as ADDITIVE — the one place this flow
must not behave like the fixture loader.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.models import ONBOARD_MESSAGE_MAX_CHARS
from openexecutive.api.routes import onboarding as route
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.onboarding import interview as iv


def _draft(**overrides: Any) -> iv.CompanyDraft:
    base: dict[str, Any] = {
        "profile": {
            "name": "Northwind Tools",
            "industry": "Industrial supply",
            "stage": "Bootstrapped",
            "mission": "Same-day tools on site.",
        },
        "people": [
            {"full_name": "Dana Reyes", "role": "CEO", "is_principal": True},
            {"full_name": "Sam Okafor", "role": "Head of Ops"},
        ],
        "departments": [
            {"title": "Operations", "mission": "Fulfilment", "head_person_name": "Sam Okafor"}
        ],
        "confidence_notes": ["Monthly burn not stated."],
        "summary": "A bootstrapped industrial supplier.",
    }
    base.update(overrides)
    return iv.CompanyDraft.model_validate(base)


@pytest.fixture()
def profile_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "company" / "profile.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    # get_settings() constructs a fresh Settings on every call, so the only
    # way to redirect the profile path is through the environment.
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(path))
    return path


@pytest.fixture()
def seeded(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Queue of advance() results; each call pops one."""
    queue: list[Any] = []

    async def _advance(transcript: list[iv.Turn], **kwargs: Any) -> Any:
        if not queue:
            raise AssertionError("advance() called more times than scripted")
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(iv, "advance", _advance)
    return queue


@pytest.fixture()
def client(
    monkeypatch: pytest.MonkeyPatch, profile_path: Path
) -> TestClient:
    fired: list[str] = []

    async def _no_research(session_id: str) -> None:
        fired.append(session_id)

    monkeypatch.setattr(route, "_fire_post_onboarding_research", _no_research)
    monkeypatch.setattr(route, "_interview_sessions", OrderedDict())
    monkeypatch.setattr(route, "_onboarding_research_fired", set())
    monkeypatch.setattr(route, "_wizard_sessions", {})
    # No people/department DB writes unless a test asks for them.
    monkeypatch.setattr(
        "openexecutive.onboarding.commit.save_onboarding_people", lambda d: {}
    )
    monkeypatch.setattr(
        "openexecutive.onboarding.commit.reconcile_onboarding_departments",
        lambda d, ids: {"updated": 0, "created": 0},
    )
    app = FastAPI()
    app.include_router(route.router)
    c = TestClient(app)
    c.fired = fired  # type: ignore[attr-defined]
    return c


def _start(client: TestClient, description: str = "We sell industrial tools.") -> str:
    resp = client.post("/onboard/interview/start", data={"description": description})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def _commit_body(session_id: str, **profile: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "session_id": session_id,
        "profile": {"name": "Northwind Tools", "industry": "Industrial supply", **profile},
        "people": [{"full_name": "Dana Reyes", "role": "CEO", "is_principal": True}],
        "departments": [{"title": "Operations", "head_person_name": "Dana Reyes"}],
    }
    return body


# ── happy path ───────────────────────────────────────────────────────────────


def test_start_with_no_description_returns_opening_prompt_without_a_model_call(
    client: TestClient, seeded: list[Any]
) -> None:
    resp = client.post("/onboard/interview/start", data={"description": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["phase"] == "question"
    assert body["question"] == iv.OPENING_PROMPT
    assert seeded == []  # advance() was never called


def test_happy_path_start_message_draft_commit(
    client: TestClient, seeded: list[Any], profile_path: Path
) -> None:
    seeded.append(iv.Question(question="Who runs ops?", hint="A name."))
    sid = _start(client)

    seeded.append(_draft())
    resp = client.post(
        "/onboard/interview/message",
        json={"session_id": sid, "message": "Sam Okafor runs ops."},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["phase"] == "draft"
    assert body["draft"]["name"] == "Northwind Tools"
    assert [p["full_name"] for p in body["draft_people"]] == ["Dana Reyes", "Sam Okafor"]
    assert body["draft_departments"][0]["title"] == "Operations"
    assert body["confidence_notes"] == ["Monthly burn not stated."]

    resp = client.post("/onboard/interview/commit", json=_commit_body(sid))
    assert resp.status_code == 200, resp.text
    assert profile_path.exists()
    saved = CompanyProfile.load_from_yaml(profile_path)
    assert saved.name == "Northwind Tools"
    assert not saved.is_empty()


def test_first_question_is_recorded_and_counted(
    client: TestClient, seeded: list[Any]
) -> None:
    seeded.append(iv.Question(question="What stage are you?", hint="Seed? Series A?"))
    resp = client.post(
        "/onboard/interview/start", data={"description": "We sell tools."}
    )
    body = resp.json()
    assert body["phase"] == "question"
    assert body["question"] == "What stage are you?"
    assert body["question_hint"] == "Seed? Series A?"
    assert body["questions_asked"] == 1
    assert body["max_questions"] == iv.MAX_QUESTIONS


def test_force_draft_endpoint(client: TestClient, seeded: list[Any]) -> None:
    seeded.append(iv.Question(question="Who runs ops?"))
    sid = _start(client)
    seeded.append(_draft())
    resp = client.post("/onboard/interview/draft", json={"session_id": sid})
    assert resp.status_code == 200
    assert resp.json()["phase"] == "draft"


def test_draft_before_any_description_is_422(
    client: TestClient, seeded: list[Any]
) -> None:
    resp = client.post("/onboard/interview/start", data={"description": ""})
    sid = resp.json()["session_id"]
    resp = client.post("/onboard/interview/draft", json={"session_id": sid})
    assert resp.status_code == 422


def test_answering_after_a_draft_clears_it_and_asks_again(
    client: TestClient, seeded: list[Any]
) -> None:
    seeded.append(_draft())
    sid = _start(client)
    assert client.get(f"/onboard/interview/{sid}").json()["phase"] == "draft"

    seeded.append(iv.Question(question="Anything else?"))
    resp = client.post(
        "/onboard/interview/message",
        json={"session_id": sid, "message": "Actually we also do rentals."},
    )
    assert resp.status_code == 200
    assert resp.json()["phase"] == "question"


def test_get_session_returns_transcript_and_draft(
    client: TestClient, seeded: list[Any]
) -> None:
    seeded.append(iv.Question(question="Who runs ops?"))
    sid = _start(client, "We sell industrial tools.")
    resp = client.get(f"/onboard/interview/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert [t["text"] for t in body["turns"]] == [
        "We sell industrial tools.",
        "Who runs ops?",
    ]
    assert body["saved"] is False


# ── no-echo guarantees ───────────────────────────────────────────────────────


def test_oversized_message_is_422_and_does_not_echo(
    client: TestClient, seeded: list[Any]
) -> None:
    seeded.append(iv.Question(question="Who runs ops?"))
    sid = _start(client)
    resp = client.post(
        "/onboard/interview/message",
        json={"session_id": sid, "message": "SECRETBURN" * 3000},
    )
    assert resp.status_code == 422
    assert "SECRETBURN" not in resp.text
    assert len(resp.content) < 500


def test_oversized_start_description_is_422_and_does_not_echo(
    client: TestClient,
) -> None:
    resp = client.post(
        "/onboard/interview/start",
        data={"description": "SECRETARR" * 3000},
    )
    assert resp.status_code == 422
    assert "SECRETARR" not in resp.text


def test_interview_error_maps_to_502_without_echoing(
    client: TestClient, seeded: list[Any]
) -> None:
    seeded.append(iv.InterviewError("The setup assistant is unavailable right now."))
    resp = client.post(
        "/onboard/interview/start", data={"description": "our burn is SECRETBURN"}
    )
    assert resp.status_code == 502
    assert "SECRETBURN" not in resp.text


def test_interview_timeout_maps_to_504(client: TestClient, seeded: list[Any]) -> None:
    seeded.append(iv.InterviewTimeout("The setup assistant took too long."))
    resp = client.post("/onboard/interview/start", data={"description": "hello"})
    assert resp.status_code == 504


def test_commit_validation_failure_is_retryable_and_non_echoing(
    client: TestClient,
    seeded: list[Any],
    profile_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected commit must leave the session usable, not stuck.

    The wizard needed a deepcopy snapshot for this because process_answer
    mutated state before the build could fail. Here the interview never
    writes, so the ordering in commit_interview is the whole guarantee.
    """
    seeded.append(_draft())
    sid = _start(client)

    real_save = CompanyProfile.save_to_yaml
    failing = {"on": True}

    def _maybe_boom(self: Any, path: Any) -> None:
        if failing["on"]:
            raise ValueError("could not convert 'SECRETRUNWAY' to float")
        real_save(self, path)

    monkeypatch.setattr(CompanyProfile, "save_to_yaml", _maybe_boom)

    resp = client.post("/onboard/interview/commit", json=_commit_body(sid))
    assert resp.status_code == 422
    assert "SECRETRUNWAY" not in resp.text
    assert not profile_path.exists()
    assert route._interview_sessions[sid].saved is False
    # The research fire is gated on a SUCCESSFUL commit.
    assert route._onboarding_research_fired == set()

    # Same session, same body — the failure was retryable.
    failing["on"] = False
    resp = client.post("/onboard/interview/commit", json=_commit_body(sid))
    assert resp.status_code == 200, resp.text
    assert profile_path.exists()
    assert route._interview_sessions[sid].saved is True


# ── commit semantics ─────────────────────────────────────────────────────────


def test_commit_with_empty_name_is_422_and_writes_nothing(
    client: TestClient, seeded: list[Any], profile_path: Path
) -> None:
    seeded.append(_draft())
    sid = _start(client)
    body = _commit_body(sid)
    body["profile"]["name"] = "   "
    resp = client.post("/onboard/interview/commit", json=body)
    assert resp.status_code == 422
    assert not profile_path.exists()


def test_second_commit_is_409_and_research_fires_once(
    client: TestClient, seeded: list[Any]
) -> None:
    seeded.append(_draft())
    sid = _start(client)
    assert client.post("/onboard/interview/commit", json=_commit_body(sid)).status_code == 200
    second = client.post("/onboard/interview/commit", json=_commit_body(sid))
    assert second.status_code == 409
    assert len(route._onboarding_research_fired) == 1


def test_commit_derives_org_structure_from_people_and_departments(
    client: TestClient, seeded: list[Any], profile_path: Path
) -> None:
    seeded.append(_draft())
    sid = _start(client)
    body = _commit_body(sid)
    body["people"] = [
        {"full_name": "Dana Reyes", "role": "CEO", "is_principal": True},
        {"full_name": "Sam Okafor", "role": "Head of Ops"},
    ]
    body["departments"] = [{"title": "Operations"}, {"title": "Finance"}]
    assert client.post("/onboard/interview/commit", json=body).status_code == 200

    saved = CompanyProfile.load_from_yaml(profile_path)
    assert saved.org_structure.departments == ["Operations", "Finance"]
    assert saved.org_structure.leadership_team == [
        "Dana Reyes, CEO",
        "Sam Okafor, Head of Ops",
    ]


def test_commit_drops_contact_fields_from_people(
    client: TestClient, seeded: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[Any] = []
    monkeypatch.setattr(
        "openexecutive.onboarding.commit.save_onboarding_people",
        lambda drafts: captured.extend(drafts) or {},
    )
    seeded.append(_draft())
    sid = _start(client)
    body = _commit_body(sid)
    body["people"] = [
        {
            "full_name": "Dana Reyes",
            "role": "CEO",
            "is_principal": True,
            "email": "dana@example.com",
            "slack_user_id": "U123",
        }
    ]
    assert client.post("/onboard/interview/commit", json=body).status_code == 200
    dumped = captured[0].model_dump()
    assert "email" not in dumped
    assert "slack_user_id" not in dumped


# ── session lifecycle ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/onboard/interview/message", {"session_id": "nope", "message": "hi"}),
        ("post", "/onboard/interview/draft", {"session_id": "nope"}),
        ("post", "/onboard/interview/commit", {"session_id": "nope", "profile": {"name": "X"}}),
    ],
)
def test_unknown_session_is_404(
    client: TestClient, method: str, path: str, payload: dict[str, Any]
) -> None:
    assert getattr(client, method)(path, json=payload).status_code == 404


def test_unknown_session_get_is_404(client: TestClient) -> None:
    assert client.get("/onboard/interview/nope").status_code == 404


def test_expired_session_is_swept(client: TestClient, seeded: list[Any]) -> None:
    seeded.append(iv.Question(question="Who runs ops?"))
    sid = _start(client)
    route._interview_sessions[sid].last_touched -= route._INTERVIEW_TTL_SECONDS + 1
    assert client.get(f"/onboard/interview/{sid}").status_code == 404
    assert sid not in route._interview_sessions


def test_session_cap_evicts_oldest(client: TestClient) -> None:
    first = _start(client, "")
    for _ in range(route._MAX_INTERVIEW_SESSIONS):
        _start(client, "")
    assert first not in route._interview_sessions
    assert len(route._interview_sessions) <= route._MAX_INTERVIEW_SESSIONS


# ── uploads ──────────────────────────────────────────────────────────────────


def test_uploaded_file_text_reaches_the_opening_turn(
    client: TestClient, seeded: list[Any]
) -> None:
    seeded.append(iv.Question(question="What stage?"))
    resp = client.post(
        "/onboard/interview/start",
        data={"description": "See the deck."},
        files=[("files", ("brief.txt", b"Northwind Tools sells industrial supplies.", "text/plain"))],
    )
    assert resp.status_code == 200, resp.text
    sid = resp.json()["session_id"]
    opening = route._interview_sessions[sid].transcript[0].text
    assert "See the deck." in opening
    assert "=== Attached: brief.txt ===" in opening
    assert "Northwind Tools sells industrial supplies." in opening


def test_unsupported_upload_is_400(client: TestClient) -> None:
    resp = client.post(
        "/onboard/interview/start",
        data={"description": "hi"},
        files=[("files", ("logo.png", b"\x89PNG\r\n", "image/png"))],
    )
    assert resp.status_code == 400
    assert "Unsupported file type" in resp.json()["detail"]


def test_too_many_uploads_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api import intake_uploads

    monkeypatch.setattr(intake_uploads, "_INTAKE_MAX_FILES", 2)
    files = [("files", (f"n{i}.txt", b"hello world", "text/plain")) for i in range(3)]
    resp = client.post("/onboard/interview/start", data={"description": "hi"}, files=files)
    assert resp.status_code == 400
    assert "Too many files" in resp.json()["detail"]


# ── the wizard fallback stays alive ──────────────────────────────────────────


def test_wizard_endpoints_still_work(client: TestClient) -> None:
    resp = client.get("/onboard/start")
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    resp = client.post("/onboard/answer", json={"session_id": sid, "answer": "Acme"})
    assert resp.status_code == 200
    assert resp.json()["current_step"] == 1
    assert client.get(f"/onboard/status/{sid}").status_code == 200


def test_wizard_answer_cap_is_unchanged(client: TestClient) -> None:
    from openexecutive.api.models import ONBOARD_ANSWER_MAX_CHARS

    assert ONBOARD_ANSWER_MAX_CHARS == 10_000
    assert ONBOARD_MESSAGE_MAX_CHARS > ONBOARD_ANSWER_MAX_CHARS


# ── regressions from the adversarial review ──────────────────────────────────


def test_draft_records_an_assistant_turn_so_the_transcript_stays_alternating(
    client: TestClient, seeded: list[Any]
) -> None:
    """Emitting a draft used to append nothing, leaving the transcript on a
    user turn. The next message then made two user turns in a row, which the
    Messages API rejects — bricking the session permanently."""
    seeded.append(iv.Question(question="Who runs ops?"))
    sid = _start(client)
    seeded.append(_draft())
    client.post(
        "/onboard/interview/message", json={"session_id": sid, "message": "Sam does."}
    )

    seeded.append(iv.Question(question="Anything else?"))
    client.post(
        "/onboard/interview/message",
        json={"session_id": sid, "message": "We also do rentals."},
    )

    roles = [t.role for t in route._interview_sessions[sid].transcript]
    assert all(a != b for a, b in zip(roles, roles[1:])), f"non-alternating: {roles}"

    # And what the real builder would send alternates and ends on a user turn
    # (never an assistant prefill, which a forced tool_choice rejects).
    built = [
        m["role"]
        for m in iv._build_messages(route._interview_sessions[sid].transcript, None)
    ]
    assert all(a != b for a, b in zip(built, built[1:])), f"non-alternating: {built}"
    assert built[-1] == "user"


def test_a_failed_follow_up_keeps_the_draft_reviewable(
    client: TestClient, seeded: list[Any]
) -> None:
    """The draft used to be cleared before the fallible call, so a 502 lost it."""
    seeded.append(_draft())
    sid = _start(client)
    assert client.get(f"/onboard/interview/{sid}").json()["phase"] == "draft"

    seeded.append(iv.InterviewError("The setup assistant is unavailable right now."))
    resp = client.post(
        "/onboard/interview/message", json={"session_id": sid, "message": "one more thing"}
    )
    assert resp.status_code == 502
    assert client.get(f"/onboard/interview/{sid}").json()["phase"] == "draft"


def test_commit_rejects_a_roster_with_no_principal(
    client: TestClient, seeded: list[Any], profile_path: Path
) -> None:
    """commit validates the CLIENT body — the UI is not the only guard."""
    seeded.append(_draft())
    sid = _start(client)
    body = _commit_body(sid)
    body["people"] = [{"full_name": "Dana Reyes", "is_principal": False}]
    body["departments"] = []
    resp = client.post("/onboard/interview/commit", json=body)
    assert resp.status_code == 422
    assert "exactly one person" in resp.json()["detail"]
    assert not profile_path.exists()


def test_commit_rejects_two_principals(client: TestClient, seeded: list[Any]) -> None:
    seeded.append(_draft())
    sid = _start(client)
    body = _commit_body(sid)
    body["people"] = [
        {"full_name": "Dana Reyes", "is_principal": True},
        {"full_name": "Sam Okafor", "is_principal": True},
    ]
    body["departments"] = []
    assert client.post("/onboard/interview/commit", json=body).status_code == 422


def test_commit_rejects_an_unknown_department_head(
    client: TestClient, seeded: list[Any]
) -> None:
    seeded.append(_draft())
    sid = _start(client)
    body = _commit_body(sid)
    body["departments"] = [{"title": "Sales", "head_person_name": "Nobody Here"}]
    resp = client.post("/onboard/interview/commit", json=body)
    assert resp.status_code == 422
    # The SAFE rendering — the detailed one quotes the rejected head name.
    assert "isn't on the team list" in resp.json()["detail"]
    assert "Nobody Here" not in resp.text


def test_commit_rejects_duplicate_person_names(
    client: TestClient, seeded: list[Any]
) -> None:
    seeded.append(_draft())
    sid = _start(client)
    body = _commit_body(sid)
    body["people"] = [
        {"full_name": "Dana Reyes", "is_principal": True},
        {"full_name": "Dana Reyes"},
    ]
    body["departments"] = []
    assert client.post("/onboard/interview/commit", json=body).status_code == 422


def test_oversized_person_name_is_422_without_echoing(
    client: TestClient, seeded: list[Any]
) -> None:
    """Bounds moved out of Field(max_length=) precisely so this cannot echo."""
    seeded.append(_draft())
    sid = _start(client)
    body = _commit_body(sid)
    body["people"] = [{"full_name": "SECRETNAME" * 40, "is_principal": True}]
    body["departments"] = []
    resp = client.post("/onboard/interview/commit", json=body)
    assert resp.status_code == 422
    assert "SECRETNAME" not in resp.text


def test_rejected_upload_leaves_no_orphan_session(client: TestClient) -> None:
    """Sessions used to be registered before the fallible upload step, so a
    rejected upload leaked one; 50 of those evicted every live session."""
    before = len(route._interview_sessions)
    resp = client.post(
        "/onboard/interview/start",
        data={"description": "hi"},
        files=[("files", ("logo.png", b"\x89PNG\r\n", "image/png"))],
    )
    assert resp.status_code == 400
    assert len(route._interview_sessions) == before


def test_overlong_transcript_refuses_more_messages(
    client: TestClient, seeded: list[Any]
) -> None:
    """The budget in interview.py only forces a draft; memory needs a real cap."""
    seeded.append(iv.Question(question="Who runs ops?"))
    sid = _start(client)
    route._interview_sessions[sid].transcript.append(
        iv.Turn(role="user", text="x" * (iv.MAX_TRANSCRIPT_CHARS + 1))
    )
    resp = client.post(
        "/onboard/interview/message", json={"session_id": sid, "message": "more"}
    )
    assert resp.status_code == 422
    assert "gotten long" in resp.json()["detail"]


def test_displayed_question_budget_matches_the_enforced_one(
    client: TestClient, seeded: list[Any]
) -> None:
    """These were two separate constants that could drift silently."""
    resp = client.post("/onboard/interview/start", data={"description": ""})
    assert resp.json()["max_questions"] == iv.MAX_QUESTIONS


def test_commit_422_never_echoes_the_rejected_value(
    client: TestClient, seeded: list[Any]
) -> None:
    """validate_draft's detailed strings quote input; the route must return the
    safe rendering, because a rejected name sits next to the financials."""
    seeded.append(_draft())
    sid = _start(client)
    body = _commit_body(sid)
    body["people"] = [
        {"full_name": "SECRETPERSON", "is_principal": True},
        {"full_name": "SECRETPERSON"},
    ]
    body["departments"] = [{"title": "Ops", "head_person_name": "SECRETHEAD"}]
    resp = client.post("/onboard/interview/commit", json=body)
    assert resp.status_code == 422
    assert "SECRETPERSON" not in resp.text
    assert "SECRETHEAD" not in resp.text


def test_failed_first_turn_leaves_no_orphan_session(
    client: TestClient, seeded: list[Any]
) -> None:
    """The 502 body carries no session_id, so the client can never resume it —
    leaving it in the dict orphans it, and a burst of provider failures would
    evict live sessions through the cap sweep."""
    before = len(route._interview_sessions)
    for _ in range(3):
        seeded.append(iv.InterviewError("The setup assistant is unavailable right now."))
        resp = client.post("/onboard/interview/start", data={"description": "hello"})
        assert resp.status_code == 502
    assert len(route._interview_sessions) == before


def test_a_full_legal_start_does_not_lock_the_conversation(
    client: TestClient, seeded: list[Any]
) -> None:
    """A big description plus the documented 8 attachments must still leave
    room to keep talking."""
    seeded.append(iv.Question(question="What stage are you?"))
    resp = client.post(
        "/onboard/interview/start",
        data={"description": "x" * 19_000},
        files=[
            ("files", (f"f{i}.txt", b"y" * 14_000, "text/plain")) for i in range(8)
        ],
    )
    assert resp.status_code == 200, resp.text
    sid = resp.json()["session_id"]

    seeded.append(iv.Question(question="And your north star?"))
    follow_up = client.post(
        "/onboard/interview/message", json={"session_id": sid, "message": "Series A."}
    )
    assert follow_up.status_code == 200, follow_up.text
