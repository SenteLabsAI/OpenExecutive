"""Unit tests for the suggest_workflow tool handler.

The handler must reject unknown workflows, reject prefilled keys not in
the workflow's input model, encode the deep link correctly, and respect
the same anti-spam / horizon / channel-ref guards as schedule_followup.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.memory.episodic import initialize_db  # noqa: E402
from openexecutive.orchestrator.schedule_tools import (  # noqa: E402
    current_session,
    handle_suggest_workflow,
)
from openexecutive.orchestrator.session import Session  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    return db_path


@pytest.fixture()
def session_with_telegram_ref() -> Iterator[Session]:
    s = Session()
    s.seen_channel_refs.add(("telegram", "42"))
    token = current_session.set(s)
    yield s
    current_session.reset(token)


def _future_iso(seconds: int = 600) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _call(tool_input: dict) -> dict:
    return json.loads(asyncio.run(handle_suggest_workflow(tool_input)))


def _b64url_decode(value: str) -> dict:
    pad = "=" * ((4 - len(value) % 4) % 4)
    raw = base64.urlsafe_b64decode((value + pad).encode("ascii"))
    return json.loads(raw.decode("utf-8"))


def test_happy_path_with_prefilled_inputs(
    session_with_telegram_ref: Session,
) -> None:
    payload = _call(
        {
            "workflow_name": "board_prep",
            "run_at": _future_iso(),
            "channel": "telegram",
            "channel_ref": "42",
            "reason": "Q2 ends in 2 weeks — want to draft the deck?",
            "prefilled_inputs": {"quarter_label": "Q2 2026"},
        }
    )
    assert payload["status"] == "scheduled"
    assert payload["workflow_name"] == "board_prep"
    assert "deep_link" in payload
    assert payload["deep_link"].endswith(
        # base64url of {"quarter_label":"Q2 2026"} sorted, no padding
        f"?prefill={_b64url_encode_known()}"
    )


def _b64url_encode_known() -> str:
    """Reference value for the prefill in the happy-path test."""
    raw = json.dumps({"quarter_label": "Q2 2026"}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode("ascii")


def test_happy_path_without_prefilled_inputs_omits_query(
    session_with_telegram_ref: Session,
) -> None:
    payload = _call(
        {
            "workflow_name": "mbr",
            "run_at": _future_iso(),
            "channel": "telegram",
            "channel_ref": "42",
            "reason": "Monthly review coming up.",
        }
    )
    assert payload["status"] == "scheduled"
    # No prefill → no query string.
    assert "?prefill=" not in payload["deep_link"]
    assert payload["deep_link"].endswith("/jobs/mbr")


def test_rejects_unknown_workflow(session_with_telegram_ref: Session) -> None:
    payload = _call(
        {
            "workflow_name": "not_a_workflow",
            "run_at": _future_iso(),
            "channel": "telegram",
            "channel_ref": "42",
            "reason": "x",
        }
    )
    assert "unknown workflow_name" in payload["error"]


def test_rejects_extra_keys_in_prefilled_inputs(
    session_with_telegram_ref: Session,
) -> None:
    payload = _call(
        {
            "workflow_name": "board_prep",
            "run_at": _future_iso(),
            "channel": "telegram",
            "channel_ref": "42",
            "reason": "x",
            "prefilled_inputs": {"quarter_label": "Q2", "bogus_field": "nope"},
        }
    )
    assert "bogus_field" in payload["error"]


def test_rejects_unseen_channel_ref(session_with_telegram_ref: Session) -> None:
    payload = _call(
        {
            "workflow_name": "board_prep",
            "run_at": _future_iso(),
            "channel": "telegram",
            "channel_ref": "999",  # not in seen_channel_refs
            "reason": "x",
        }
    )
    assert "not seen in this session" in payload["error"]


def test_rejects_past_run_at(session_with_telegram_ref: Session) -> None:
    payload = _call(
        {
            "workflow_name": "board_prep",
            "run_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            "channel": "telegram",
            "channel_ref": "42",
            "reason": "x",
        }
    )
    assert "must be in the future" in payload["error"]


def test_rejects_empty_reason(session_with_telegram_ref: Session) -> None:
    payload = _call(
        {
            "workflow_name": "board_prep",
            "run_at": _future_iso(),
            "channel": "telegram",
            "channel_ref": "42",
            "reason": "   ",
        }
    )
    assert "reason must not be empty" in payload["error"]


def test_rejects_prefilled_inputs_non_dict(
    session_with_telegram_ref: Session,
) -> None:
    payload = _call(
        {
            "workflow_name": "board_prep",
            "run_at": _future_iso(),
            "channel": "telegram",
            "channel_ref": "42",
            "reason": "x",
            "prefilled_inputs": ["not", "an", "object"],
        }
    )
    assert "must be an object" in payload["error"]


def test_rejects_non_scalar_prefill_leaves(
    session_with_telegram_ref: Session,
) -> None:
    # A nested object that contains a non-JSON-scalar leaf — the
    # validator should catch it BEFORE we ever try to json.dumps it.
    # This guards against the Executive smuggling in a datetime, set,
    # bytes, or model instance that would otherwise raise TypeError
    # outside the handler's try/except.
    class _Unserializable:
        pass

    payload = _call(
        {
            "workflow_name": "board_prep",
            "run_at": _future_iso(),
            "channel": "telegram",
            "channel_ref": "42",
            "reason": "x",
            "prefilled_inputs": {"quarter_label": _Unserializable()},  # type: ignore[dict-item]
        }
    )
    assert "unsupported type" in payload["error"]
    assert "quarter_label" in payload["error"]


def test_rejects_oversize_prefill(session_with_telegram_ref: Session) -> None:
    # Even with all-string scalars, the serialized JSON is capped to
    # ~2 KB so the resulting URL stays deliverable over email/Telegram.
    big_string = "x" * 4096
    payload = _call(
        {
            "workflow_name": "board_prep",
            "run_at": _future_iso(),
            "channel": "telegram",
            "channel_ref": "42",
            "reason": "x",
            "prefilled_inputs": {"quarter_label": big_string},
        }
    )
    assert "exceeds cap" in payload["error"]


def test_rejects_reason_over_cap(session_with_telegram_ref: Session) -> None:
    payload = _call(
        {
            "workflow_name": "board_prep",
            "run_at": _future_iso(),
            "channel": "telegram",
            "channel_ref": "42",
            "reason": "y" * 1000,
        }
    )
    assert "reason must be" in payload["error"]


def test_intent_text_contains_deep_link_verbatim(
    session_with_telegram_ref: Session, isolated_db: Path
) -> None:
    # The deep link is the load-bearing artifact in the runner-Executive's
    # eventual message — confirm it lives verbatim inside the stored
    # intent_text AND comes before the reason (so any truncation hits the
    # reason, not the URL). If this invariant ever flips, the runner may
    # send a chopped URL and the suggestion link becomes unusable.
    from openexecutive.memory.episodic import _get_conn

    reason = "Monthly review coming up — please draft the May MBR."
    payload = _call(
        {
            "workflow_name": "mbr",
            "run_at": _future_iso(),
            "channel": "telegram",
            "channel_ref": "42",
            "reason": reason,
            "prefilled_inputs": {"month_label": "May 2026"},
        }
    )
    assert payload["status"] == "scheduled"
    expected_link = payload["deep_link"]
    with _get_conn(isolated_db) as conn:
        row = conn.execute(
            "SELECT intent_text FROM scheduled_actions WHERE id = ?",
            (payload["id"],),
        ).fetchone()
    assert row is not None
    intent_text = row["intent_text"]
    assert expected_link in intent_text
    assert reason in intent_text
    # Load-bearing ordering: URL must come before reason so truncation
    # chops the reason, not the URL.
    assert intent_text.index(expected_link) < intent_text.index(reason)


def test_validates_nested_non_scalar_leaves(
    session_with_telegram_ref: Session,
) -> None:
    # _validate_prefill_leaves must recurse through dicts and lists. A
    # bug that special-cased only the top level would slip through this.
    class _Unserializable:
        pass

    # Hide the bad value inside a nested list of dicts. The whitelisted
    # top-level key is `decisions_needed` on board_prep (it's a string
    # field, but the handler accepts any value type and lets the validator
    # walk the structure — that's intentional, so non-string Executive
    # output gets caught before json.dumps).
    payload = _call(
        {
            "workflow_name": "board_prep",
            "run_at": _future_iso(),
            "channel": "telegram",
            "channel_ref": "42",
            "reason": "x",
            "prefilled_inputs": {
                "decisions_needed": [
                    {"item": "ok"},
                    {"item": _Unserializable()},  # type: ignore[dict-item]
                ]
            },
        }
    )
    assert "unsupported type" in payload["error"]


def test_rejects_deeply_nested_prefill(
    session_with_telegram_ref: Session,
) -> None:
    # Construct a dict nested far deeper than the cap so the depth-limit
    # guard fires before Python's recursion limit does.
    deep: Any = "leaf"
    for _ in range(20):
        deep = {"x": deep}
    payload = _call(
        {
            "workflow_name": "board_prep",
            "run_at": _future_iso(),
            "channel": "telegram",
            "channel_ref": "42",
            "reason": "x",
            "prefilled_inputs": {"decisions_needed": deep},
        }
    )
    assert "nested deeper than" in payload["error"]


def test_ui_base_url_validator_rejects_bad_schemes() -> None:
    # Belt-and-suspenders: the deep-link cap inside the handler protects
    # against ui_base_url that's huge or weird. But the first line of
    # defense is the config-load validator. Confirm it rejects non-http(s)
    # and over-cap base URLs so misconfiguration is caught at startup.
    from openexecutive.config import Settings

    with pytest.raises(ValidationError):  # Pydantic wraps the ValueError
        Settings(UI_BASE_URL="javascript:alert(1)")  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        Settings(UI_BASE_URL="not-a-url")  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        Settings(UI_BASE_URL="https://" + ("x" * 600))  # type: ignore[call-arg]

    # Happy path still works.
    s = Settings(UI_BASE_URL="https://app.example.com")  # type: ignore[call-arg]
    assert s.ui_base_url == "https://app.example.com"
