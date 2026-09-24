"""Tests for the inbound resolver (3-tier WaitForHuman matching)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openexecutive.memory import episodic
from openexecutive.workflows import persistence as wf_persistence
from openexecutive.workflows.inbound_resolver import resolve_inbound_message
from openexecutive.workflows.wait_for_human import WaitForHumanResolution


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "test.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(wf_persistence, "DB_PATH", db)
    episodic.initialize_db(db)
    wf_persistence.initialize_runs_db(db)
    yield


def _seed_awaiting_run(
    run_id: str,
    person_id: int,
    channel: str = "",
    outbound_message_id: str = "",
    origin_session_id: str = "",
    delivery: str | None = None,
    *,
    db: Path,
) -> None:
    """Seed one awaiting_human run.

    The defaults are the shape production actually wrote before gate delivery
    existed: NO channel and NO outbound_message_id. The previous defaults here
    (`channel="slack"` plus a message id) described a state_json that
    `dynamic.py` never produced, which is why the resolver's dead tiers stayed
    green in CI while nothing resolved in the field (#136). Tests that want the
    populated shape now ask for it explicitly.
    """
    wf_persistence.create_run(run_id, "test_wf", "Test", {}, db_path=db)
    state_dict = {
        "on_timeout": "escalate",
        "channel": channel,
        "channel_ref": "U123",
        "outbound_message_id": outbound_message_id,
        "origin_session_id": origin_session_id,
        "expected_reply_shape": "approve_reject",
        "question": "Please approve this vendor renegotiation.",
    }
    # Omitted entirely for a legacy row — its ABSENCE is what marks a
    # checkpoint written before gate delivery existed.
    if delivery is not None:
        state_dict["delivery"] = delivery
    state = json.dumps(state_dict)
    until = datetime.now(UTC) + timedelta(hours=48)
    wf_persistence.save_checkpoint(run_id, state, person_id, until, db_path=db)


def _run_resolver(**kwargs) -> WaitForHumanResolution | None:
    return asyncio.run(resolve_inbound_message(**kwargs))


# ---------------------------------------------------------------------------
# No candidates
# ---------------------------------------------------------------------------

def test_resolve_returns_none_when_no_candidates(tmp_path: Path) -> None:
    result = _run_resolver(
        channel="slack",
        channel_ref="U123",
        from_person_id=1,
        text="approved",
    )
    assert result is None


def test_resolve_returns_none_for_wrong_person(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=10, db=db)
    result = _run_resolver(
        channel="slack",
        channel_ref="U123",
        from_person_id=99,  # different person
        text="approved",
    )
    assert result is None


# ---------------------------------------------------------------------------
# Tier 1: explicit in_reply_to
# ---------------------------------------------------------------------------

def test_resolve_tier1_explicit_message_id(tmp_path: Path) -> None:
    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=5, outbound_message_id="msg-abc", db=db)

    with patch("openexecutive.workflows.inbound_resolver.parse_decision", new=AsyncMock(return_value={"decision": "approve", "note": ""})):
        result = _run_resolver(
            channel="slack",
            channel_ref="U123",
            from_person_id=5,
            text="yes approved",
            in_reply_to="msg-abc",
        )
    assert result is not None
    assert result.run_id == "run-1"
    assert result.source_channel == "slack"
    assert result.person_id == 5


# ---------------------------------------------------------------------------
# Tier 2: single candidate
# ---------------------------------------------------------------------------

def test_resolve_by_single_candidate_match(tmp_path: Path) -> None:
    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=7, channel="slack", db=db)

    with patch("openexecutive.workflows.inbound_resolver.parse_decision", new=AsyncMock(return_value={"decision": "approve", "note": ""})):
        result = _run_resolver(
            channel="slack",
            channel_ref="U123",
            from_person_id=7,
            text="sounds good",
        )
    assert result is not None
    assert result.run_id == "run-1"
    assert result.person_id == 7


def test_resolve_single_candidate_parses_decision(tmp_path: Path) -> None:
    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=3, channel="slack", db=db)

    with patch("openexecutive.workflows.inbound_resolver.parse_decision", new=AsyncMock(return_value={"decision": "reject", "note": "too expensive"})):
        result = _run_resolver(
            channel="slack",
            channel_ref="U123",
            from_person_id=3,
            text="no reject",
        )
    assert result is not None
    assert result.parsed_decision == {"decision": "reject", "note": "too expensive"}


# ---------------------------------------------------------------------------
# Tier 3: multiple candidates — LLM disabled path
# ---------------------------------------------------------------------------

def test_resolve_returns_none_when_multiple_candidates_no_llm(tmp_path: Path) -> None:
    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=2, channel="slack", db=db)
    _seed_awaiting_run("run-2", person_id=2, channel="slack", db=db)

    with patch(
        "openexecutive.workflows.inbound_resolver._llm_disambiguate",
        return_value=(None, 0.5),  # low confidence
    ):
        result = _run_resolver(
            channel="slack",
            channel_ref="U123",
            from_person_id=2,
            text="ok",
        )
    assert result is None


def test_resolve_tier3_high_confidence_returns_match(tmp_path: Path) -> None:
    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=2, channel="slack", db=db)
    _seed_awaiting_run("run-2", person_id=2, channel="slack", db=db)

    with patch(
        "openexecutive.workflows.inbound_resolver._llm_disambiguate",
        return_value=("run-2", 0.92),  # above threshold
    ), patch("openexecutive.workflows.inbound_resolver.parse_decision", new=AsyncMock(return_value={"decision": "approve", "note": ""})):
        result = _run_resolver(
            channel="slack",
            channel_ref="U123",
            from_person_id=2,
            text="yes this one",
        )
    assert result is not None
    assert result.run_id == "run-2"


# ---------------------------------------------------------------------------
# parse_decision (unit)
# ---------------------------------------------------------------------------

def _fake_provider(text: str | None = None, *, error: bool = False) -> MagicMock:
    """Provider stub whose async messages_create returns a content block
    carrying ``text`` (or raises, when error=True)."""
    provider = MagicMock()
    if error:
        provider.messages_create = AsyncMock(side_effect=RuntimeError("api down"))
    else:
        resp = type("Resp", (), {"content": [type("Block", (), {"text": text})()]})()
        provider.messages_create = AsyncMock(return_value=resp)
    return provider


def test_parse_decision_approve_reject() -> None:
    from openexecutive.workflows.wait_for_human import parse_decision
    provider = _fake_provider('{"decision": "approve", "note": "looks good"}')
    with patch("openexecutive.providers.get_provider", return_value=provider):
        result = asyncio.run(parse_decision("yes approved", "approve_reject"))
    assert result["decision"] == "approve"
    assert "note" in result


def test_parse_decision_free_text() -> None:
    from openexecutive.workflows.wait_for_human import parse_decision
    provider = _fake_provider('{"text": "The Q1 numbers are in the attachment"}')
    with patch("openexecutive.providers.get_provider", return_value=provider):
        result = asyncio.run(parse_decision("Q1 numbers attached", "free_text"))
    assert "text" in result


def test_parse_decision_falls_back_on_api_error() -> None:
    from openexecutive.workflows.wait_for_human import parse_decision
    provider = _fake_provider(error=True)
    with patch("openexecutive.providers.get_provider", return_value=provider):
        result = asyncio.run(parse_decision("yes", "approve_reject"))
    assert isinstance(result, dict)
    assert result["decision"] == "defer"


# ---------------------------------------------------------------------------
# Reachability (#136) — the tiers that were dead in production
# ---------------------------------------------------------------------------

_APPROVE = {"decision": "approve", "note": ""}


def test_tier1_falls_through_when_no_candidate_has_an_outbound_id() -> None:
    """Slack passes its thread_ts as in_reply_to on every threaded message.

    Tier 1 treated any non-empty in_reply_to as an explicit reference and
    refused to fall through when it matched nothing — so with the production
    state shape (no stored outbound id) Slack never reached tiers 2 or 3 and
    no approval could ever land.
    """
    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=7, channel="slack", db=db)

    with patch(
        "openexecutive.workflows.inbound_resolver.parse_decision",
        new=AsyncMock(return_value=_APPROVE),
    ):
        result = _run_resolver(
            channel="slack",
            channel_ref="U123",
            from_person_id=7,
            text="yes, go ahead",
            in_reply_to="1700000000.001",  # a Slack thread_ts, not a reply ref
        )

    assert result is not None
    assert result.run_id == "run-1"


def test_tier1_still_refuses_to_guess_when_a_real_reference_exists() -> None:
    """When a candidate DOES carry an outbound id, a mismatched explicit
    reference must not fall through to fuzzier matching."""
    db = episodic.DB_PATH
    _seed_awaiting_run(
        "run-1", person_id=7, channel="slack", outbound_message_id="msg-abc", db=db
    )

    result = _run_resolver(
        channel="slack",
        channel_ref="U123",
        from_person_id=7,
        text="yes",
        in_reply_to="some-other-message",
    )

    assert result is None


def test_tier2_matches_a_run_with_no_stored_channel() -> None:
    """Rows checkpointed before gate delivery populated `channel` would
    otherwise be permanently unmatchable — the tier-2 filter compared against
    an empty string."""
    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=7, channel="", db=db)

    with patch(
        "openexecutive.workflows.inbound_resolver.parse_decision",
        new=AsyncMock(return_value=_APPROVE),
    ):
        result = _run_resolver(
            channel="slack", channel_ref="U123", from_person_id=7, text="approved"
        )

    assert result is not None


def test_tier2_normalizes_outbound_channel_names() -> None:
    """`slack_dm` (outbound vocabulary) and `slack` (inbound) are one channel."""
    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=7, channel="slack_dm", db=db)

    with patch(
        "openexecutive.workflows.inbound_resolver.parse_decision",
        new=AsyncMock(return_value=_APPROVE),
    ):
        result = _run_resolver(
            channel="slack", channel_ref="U123", from_person_id=7, text="approved"
        )

    assert result is not None


def test_tier2_ignores_a_run_from_a_different_channel() -> None:
    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=7, channel="telegram", db=db)

    result = _run_resolver(
        channel="slack", channel_ref="U123", from_person_id=7, text="approved"
    )

    assert result is None


# --- session scoping ------------------------------------------------------ #


def test_session_scoped_gate_matches_only_its_own_conversation() -> None:
    db = episodic.DB_PATH
    _seed_awaiting_run(
        "run-1",
        person_id=7,
        channel="slack",
        origin_session_id="slack:dm:U123",
        db=db,
    )

    with patch(
        "openexecutive.workflows.inbound_resolver.parse_decision",
        new=AsyncMock(return_value=_APPROVE),
    ):
        matched = _run_resolver(
            channel="slack",
            channel_ref="U123",
            from_person_id=7,
            text="approved",
            session_ids=["slack:dm:U123"],
        )
        other_thread = _run_resolver(
            channel="slack",
            channel_ref="U123",
            from_person_id=7,
            text="approved",
            session_ids=["slack:thread:C1:1700000000.0"],
        )

    assert matched is not None
    assert other_thread is None


def test_unscoped_gate_still_matches_any_session() -> None:
    """Web- and scheduler-originated gates have no conversation to tie to."""
    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=7, channel="slack", db=db)

    with patch(
        "openexecutive.workflows.inbound_resolver.parse_decision",
        new=AsyncMock(return_value=_APPROVE),
    ):
        result = _run_resolver(
            channel="slack",
            channel_ref="U123",
            from_person_id=7,
            text="approved",
            session_ids=["slack:thread:C1:1700000000.0"],
        )

    assert result is not None


# --- decision-shape guards ------------------------------------------------ #


def test_unrelated_message_does_not_resolve_the_gate() -> None:
    """With an open gate, EVERY message from that person reaches the resolver.

    Answering "what's on my calendar?" with "your response has been recorded"
    — and silently closing the sign-off — would be worse than never resolving.
    """
    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=7, channel="slack", db=db)

    with patch(
        "openexecutive.workflows.inbound_resolver.parse_decision",
        new=AsyncMock(return_value={"decision": "unrelated", "note": "new topic"}),
    ):
        result = _run_resolver(
            channel="slack",
            channel_ref="U123",
            from_person_id=7,
            text="what's on my calendar tomorrow?",
        )

    assert result is None


def test_parser_failure_leaves_the_gate_open() -> None:
    """parse_decision's fallback is a fabricated answer, not a verdict.
    Recording it would close a sign-off on a parser outage."""
    from openexecutive.workflows.wait_for_human import PARSE_FAILED_KEY

    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=7, channel="slack", db=db)

    with patch(
        "openexecutive.workflows.inbound_resolver.parse_decision",
        new=AsyncMock(
            return_value={"decision": "defer", "note": "x", PARSE_FAILED_KEY: True}
        ),
    ):
        result = _run_resolver(
            channel="slack", channel_ref="U123", from_person_id=7, text="approved"
        )

    assert result is None


def test_a_model_written_parse_error_note_is_not_mistaken_for_the_fallback() -> None:
    """The sentinel used to be `note == "parse_error"`, which the model can
    emit — a person replying "no, your parser threw a parse_error" would have
    had their genuine rejection silently discarded."""
    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=7, channel="slack", db=db)

    with patch(
        "openexecutive.workflows.inbound_resolver.parse_decision",
        new=AsyncMock(
            return_value={"decision": "reject", "note": "parse_error mentioned"}
        ),
    ):
        result = _run_resolver(
            channel="slack",
            channel_ref="U123",
            from_person_id=7,
            text="no — your parser threw a parse_error on my last message",
        )

    assert result is not None
    assert result.parsed_decision["decision"] == "reject"


def test_parser_failure_leaves_a_free_text_gate_open_too() -> None:
    """The guard used to run only for approve_reject, so a parser outage on a
    free_text gate recorded an empty string as the answer and closed it."""
    from openexecutive.workflows.wait_for_human import PARSE_FAILED_KEY

    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=7, channel="slack", db=db)

    with patch(
        "openexecutive.workflows.inbound_resolver.parse_decision",
        new=AsyncMock(return_value={"text": "", PARSE_FAILED_KEY: True}),
    ):
        result = _run_resolver(
            channel="slack", channel_ref="U123", from_person_id=7, text="anything"
        )

    assert result is None


def test_the_fallback_marker_is_not_stored_on_the_resolution() -> None:
    """It is internal plumbing, not part of the recorded decision."""
    from openexecutive.workflows.wait_for_human import PARSE_FAILED_KEY

    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=7, channel="slack", db=db)

    with patch(
        "openexecutive.workflows.inbound_resolver.parse_decision",
        new=AsyncMock(return_value=dict(_APPROVE)),
    ):
        result = _run_resolver(
            channel="slack", channel_ref="U123", from_person_id=7, text="approved"
        )

    assert result is not None
    assert PARSE_FAILED_KEY not in result.parsed_decision


def test_a_genuine_defer_still_resolves() -> None:
    """Only the parse_error fallback is rejected — a real 'defer' is an answer."""
    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=7, channel="slack", db=db)

    with patch(
        "openexecutive.workflows.inbound_resolver.parse_decision",
        new=AsyncMock(return_value={"decision": "defer", "note": "need numbers"}),
    ):
        result = _run_resolver(
            channel="slack", channel_ref="U123", from_person_id=7, text="not yet"
        )

    assert result is not None
    assert result.parsed_decision["decision"] == "defer"


def test_undelivered_gate_is_not_wildcard_matched() -> None:
    """A gate whose delivery was suppressed / alerted / failed has no stored
    channel — the same shape as a legacy row. It must NOT be matched loosely:
    nobody was asked on any channel, so matching would record an answer to a
    question that was never put."""
    db = episodic.DB_PATH
    _seed_awaiting_run(
        "run-1", person_id=7, channel="", delivery="suppressed", db=db
    )

    result = _run_resolver(
        channel="telegram",
        channel_ref="556677",
        from_person_id=7,
        text="what's on my plate?",
    )

    assert result is None


def test_legacy_row_without_a_delivery_key_is_still_wildcard_matched() -> None:
    """Rows checkpointed before gate delivery existed carry no `delivery` key
    at all — those stay answerable."""
    db = episodic.DB_PATH
    _seed_awaiting_run("run-1", person_id=7, channel="", delivery=None, db=db)

    with patch(
        "openexecutive.workflows.inbound_resolver.parse_decision",
        new=AsyncMock(return_value=_APPROVE),
    ):
        result = _run_resolver(
            channel="telegram", channel_ref="556677", from_person_id=7, text="ok"
        )

    assert result is not None


def test_tier1_rejection_does_not_block_a_sibling_gate_with_no_outbound_id() -> None:
    """The predicate was a global `any()`: one delivered gate's outbound id
    blocked an unrelated self-approval gate from ever being answered."""
    db = episodic.DB_PATH
    _seed_awaiting_run(
        "delivered", person_id=7, channel="slack", outbound_message_id="T_a", db=db
    )
    _seed_awaiting_run("self-gate", person_id=7, channel="slack", db=db)

    with patch(
        "openexecutive.workflows.inbound_resolver.parse_decision",
        new=AsyncMock(return_value=_APPROVE),
    ):
        result = _run_resolver(
            channel="slack",
            channel_ref="U123",
            from_person_id=7,
            text="approved",
            in_reply_to="T_x",  # matches neither
        )

    # The delivered gate is ruled out by the unmatched reference; the self
    # gate is the only candidate left, so tier 2 takes it.
    assert result is not None
    assert result.run_id == "self-gate"


def test_a_gate_accepts_an_aliased_session_id() -> None:
    """A Slack channel mention raises its gate under the rolling channel
    session, but the answer arrives in the thread the bot's reply rooted —
    a different id. The adapter offers both."""
    db = episodic.DB_PATH
    _seed_awaiting_run(
        "run-1",
        person_id=7,
        channel="slack",
        origin_session_id="slack:channel:C1:U123",
        db=db,
    )

    with patch(
        "openexecutive.workflows.inbound_resolver.parse_decision",
        new=AsyncMock(return_value=_APPROVE),
    ):
        result = _run_resolver(
            channel="slack",
            channel_ref="U123",
            from_person_id=7,
            text="approved",
            session_ids=[
                "slack:thread:C1:1700000000.0",
                "slack:channel:C1:U123",
            ],
        )

    assert result is not None


def test_unreadable_state_json_is_not_wildcard_matched() -> None:
    """A corrupt row has no channel and no `delivery` key — the same shape as
    a legacy row — so the compat wildcard would have matched any message from
    that person on any channel."""
    db = episodic.DB_PATH
    wf_persistence.create_run("run-1", "test_wf", "Test", {}, db_path=db)
    until = datetime.now(UTC) + timedelta(hours=48)
    wf_persistence.save_checkpoint("run-1", "{not json", 7, until, db_path=db)

    result = _run_resolver(
        channel="slack", channel_ref="U123", from_person_id=7, text="approved"
    )

    assert result is None


def test_a_dm_scoped_gate_is_not_answerable_from_a_public_channel() -> None:
    """A gate delivered by DM records that DM's session. Before that it had
    no session at all and matched on (person, channel) alone, so an unrelated
    @mention in a public channel resolved it — and the acknowledgement, run
    title included, was posted there."""
    db = episodic.DB_PATH
    _seed_awaiting_run(
        "run-1",
        person_id=7,
        channel="slack",
        origin_session_id="slack:dm:U123",
        delivery="sent",
        db=db,
    )

    public = _run_resolver(
        channel="slack",
        channel_ref="U123",
        from_person_id=7,
        text="sounds good",
        session_ids=["slack:channel:C1:U123"],
    )
    assert public is None

    with patch(
        "openexecutive.workflows.inbound_resolver.parse_decision",
        new=AsyncMock(return_value=_APPROVE),
    ):
        in_dm = _run_resolver(
            channel="slack",
            channel_ref="U123",
            from_person_id=7,
            text="sounds good",
            session_ids=["slack:dm:U123"],
        )
    assert in_dm is not None
