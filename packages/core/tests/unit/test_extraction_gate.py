"""The turn gate in front of the episodic memory extractor.

Regression set for an install where the extractor ran 13 times, cost real
money, and stored nothing. The gate was a combined user+assistant floor of
1500 chars, and on real traffic it selected almost exactly the wrong turns:
of 16 exchanges it blocked 9, and those 9 held every instruction the principal
gave, while the 7 it admitted were long analytical exchanges with no
commitment in them at all.

The turns below are the real ones, with their real lengths.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, NamedTuple
from unittest import mock

import pytest

from openexecutive.memory import episodic
from openexecutive.memory.episodic import initialize_db


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """An isolated episodic DB.

    Without it these passes write real rows into `./episodic_memory.db`, which
    other modules then read back as production data — the audit-log pollution
    trap in CLAUDE.md.
    """
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    return db_path

# Real turns from the tenant, with the assistant length that accompanied them.
# Each is the principal giving an instruction, and each was discarded by the
# combined-length floor.
_COMMITMENTS = [
    ("It's a mistake on the lp purchased properties worksheet.  That's it. "
     "Correct it there. And record this as fixed.", 1200),
    ("Nope.  The briefing is incorrect.  We are not under water 2.9m that is "
     "just how the finicals look as received Q2", 920),
    ("This is not relevant for us.  Don't track this", 864),
    ("Well remember you fixed it or something. So we don't keep getting. "
     "Notified.", 612),
]

# The shape a length floor cannot handle: a long analysis answered in a few
# words. A floor high enough to skip "Done" also skips "Do B.".
_SHORT_APPROVALS = ["Approve option B.", "Do B.", "Kill it. Approved.", "Yes, ship it."]


@pytest.mark.parametrize("message,assistant_len", _COMMITMENTS)
def test_real_commitments_reach_the_extractor(message: str, assistant_len: int) -> None:
    """Drives the real gate, not arithmetic on literals."""
    assert len(message) + assistant_len < 1500, "blocked by the old combined floor"
    assert episodic.should_extract(message)


@pytest.mark.parametrize("message", _SHORT_APPROVALS)
def test_short_approvals_reach_the_extractor(message: str) -> None:
    """The canonical executive decision: long analysis, three-word answer.

    A user-side length floor regressed these — "Approve option B." is 17 chars
    and the old combined floor DID admit it after a long reply. Any floor that
    skips "Done" (4) also skips "Do B." (5), so there is no floor.
    """
    assert episodic.should_extract(message)


def test_empty_turns_are_skipped() -> None:
    for blank in ("", "   ", "\n\t "):
        assert not episodic.should_extract(blank)


class _Stores(NamedTuple):
    """The three persistence calls, mocked so a test can assert on arguments."""

    decision: mock.MagicMock
    initiative: mock.MagicMock
    advice: mock.MagicMock


# --------------------------------------------------------------------- #
# Who is speaking
#
# `_is_valid_user_commitment` tests a quote against `user_message`, which is
# only meaningful when that text is the principal's own words. On a chat
# channel it is not: `Executive.chat()` is reached from Slack, Discord,
# Telegram, Google Chat and the email poller, and the message there belongs to
# whoever sent it. Removing the length floor is what makes this bite — short
# channel traffic used to fall under it incidentally.
# --------------------------------------------------------------------- #


def _person(*, is_principal: bool) -> mock.MagicMock:
    person = mock.MagicMock()
    person.is_principal = is_principal
    return person


def test_a_web_turn_needs_no_person_row() -> None:
    """No origin channel means the web app, the CLI or the API — the
    principal's own authenticated surfaces. `person_id` is legitimately None
    there in a single-user install, which is the tenant this fix is for."""
    assert episodic.should_extract("Do B.", origin_channel="", person_id=None)


@pytest.mark.parametrize("channel", ["slack", "discord", "telegram", "email"])
def test_a_channel_turn_from_the_principal_is_extracted(channel: str) -> None:
    with mock.patch(
        "openexecutive.people.store.get_person", return_value=_person(is_principal=True)
    ):
        assert episodic.should_extract(
            "Do B.", origin_channel=channel, person_id=7
        )


@pytest.mark.parametrize("channel", ["slack", "discord", "telegram", "email"])
def test_a_channel_turn_from_anyone_else_is_skipped(channel: str) -> None:
    """A teammate's line would land in `decisions` with no speaker attached,
    indistinguishable from the principal's own commitment."""
    with mock.patch(
        "openexecutive.people.store.get_person",
        return_value=_person(is_principal=False),
    ):
        assert not episodic.should_extract(
            "Let's move the deadline to Friday.", origin_channel=channel, person_id=9
        )


def test_an_unidentified_channel_speaker_is_skipped() -> None:
    """An inbound email is text the sender chose, so a verbatim self-quote is
    free. With no resolved person there is nothing to check it against."""
    assert not episodic.should_extract(
        "I approve the wire transfer.", origin_channel="email", person_id=None
    )


def test_a_failed_person_lookup_fails_closed() -> None:
    with mock.patch(
        "openexecutive.people.store.get_person", side_effect=RuntimeError("no table")
    ):
        assert not episodic.should_extract(
            "Do B.", origin_channel="slack", person_id=7
        )


def test_a_missing_person_row_fails_closed() -> None:
    with mock.patch("openexecutive.people.store.get_person", return_value=None):
        assert not episodic.should_extract(
            "Do B.", origin_channel="slack", person_id=7
        )


class _Pass(NamedTuple):
    stores: _Stores
    audit: list[dict[str, Any]]
    create: mock.AsyncMock
    """The provider's `messages_create` — call count and prompts of both passes."""


def _tool_response(payload: Any) -> Any:
    class _Block:
        type = "tool_use"
        name = "store_memories"
        input = payload

    class _Response:
        content = [_Block()]

    return _Response()


# Sentinel: the retry (if any) replays the first payload, as a single
# `return_value` mock does. Tests that exercise the retry pass a payload.
_REPLAY = object()

# The `retry` block of a pass that owed no corrective call — the code's own
# shape, so a renamed key fails these tests at the cause rather than drifting.
_NO_RETRY = episodic._no_retry()


async def _run_pass(
    payload: Any,
    db_path: Path,
    *,
    user_message: str = "Cut burn to 400k. Tell the team.",
    retry_payload: Any = _REPLAY,
) -> tuple[_Stores, list[dict[str, Any]]]:
    """`_run_pass_full` for tests that only read the stores and the audit row."""
    result = await _run_pass_full(
        payload, db_path, user_message=user_message, retry_payload=retry_payload
    )
    return result.stores, result.audit


async def _run_pass_full(
    payload: Any,
    db_path: Path,
    *,
    user_message: str = "Cut burn to 400k. Tell the team.",
    retry_payload: Any = _REPLAY,
) -> _Pass:
    """Drive one real extraction pass over a model payload.

    The payload is whatever the model put in its `store_memories` tool block —
    it is NOT schema-checked anywhere, so tests hand it the shapes a model
    actually emits, including the malformed ones.

    `retry_payload` is what the model answers on the corrective pass that a
    `bad_quote` drop triggers: a payload, a whole response object (anything
    with `.content`), or an exception instance to raise.
    Left alone, the mock replays `payload`, so a test with a bad quote and no
    interest in the retry sees the item rejected a second time.

    All three store functions are mocked, so every kind is asserted the same
    way — on the arguments it was called with, not just on the audit counters.
    `db_path` still points at an isolated DB so nothing here can reach the
    default `./episodic_memory.db` if a code path stops going through them.
    """
    rows: list[dict[str, Any]] = []

    def _capture(event_type: str, summary: str, **kw: Any) -> None:
        rows.append({"event_type": event_type, "summary": summary, **kw})

    with (
        mock.patch.object(episodic, "store_decision") as store_decision,
        mock.patch.object(episodic, "store_initiative") as store_initiative,
        mock.patch.object(episodic, "store_advice") as store_advice,
        mock.patch("openexecutive.audit.log_event", _capture),
        mock.patch("openexecutive.audit.usage.log_model_usage"),
        mock.patch("openexecutive.config.get_settings"),
        mock.patch.object(episodic, "get_active_initiatives", return_value=[]),
        mock.patch("openexecutive.providers.get_provider") as provider,
    ):
        if retry_payload is _REPLAY:
            create = mock.AsyncMock(return_value=_tool_response(payload))
        else:
            if isinstance(retry_payload, BaseException) or hasattr(retry_payload, "content"):
                second = retry_payload
            else:
                second = _tool_response(retry_payload)
            create = mock.AsyncMock(side_effect=[_tool_response(payload), second])
        provider.return_value.messages_create = create
        await episodic.extract_and_store(
            user_message, "Understood.", db_path=db_path, session_id="s-1"
        )

    return _Pass(
        _Stores(store_decision, store_initiative, store_advice),
        [r for r in rows if r["event_type"] == "memory_extraction"],
        create,
    )


@pytest.mark.asyncio
async def test_extraction_audits_proposed_stored_and_dropped(db: Path) -> None:
    """A pass that proposes items and stores none must be visible.

    That is exactly what happened in production, and it was indistinguishable
    from "the model found nothing" because drops were logger.debug and stores
    wrote no row.
    """
    stores, audit = await _run_pass(db_path=db, payload={
        "decisions": [
            # Valid: the quote is the user's own words, terminated.
            {"domain": "finance", "summary": "Cut burn to 400k",
             "user_commitment_quote": "Cut burn to 400k"},
            # Invalid: quote is not in the user message at all.
            {"domain": "finance", "summary": "Sell the building",
             "user_commitment_quote": "sell the building"},
        ],
    }, retry_payload={})  # the corrective pass finds nothing to re-quote

    assert stores.decision.call_count == 1
    assert stores.decision.call_args.kwargs["summary"] == "Cut burn to 400k", (
        "the item that survived must be the one with a valid quote"
    )
    assert len(audit) == 1
    assert audit[0]["details"]["proposed"]["decisions"] == 2
    assert audit[0]["details"]["stored"]["decisions"] == 1
    assert audit[0]["details"]["dropped_count"] == 1
    assert audit[0]["details"]["dropped"] == [
        {"kind": "decision", "reason": "bad_quote", "label": "Sell the building"}
    ]
    assert audit[0]["details"]["failure"] == ""
    assert audit[0]["details"]["retry"] == {
        **_NO_RETRY, "rejected": 1,
    }


# --------------------------------------------------------------------- #
# Malformed model payloads
#
# `block.input` is whatever the model emitted; nothing validates its shape
# before the loops index into it. A `null` or a list of bare strings used to
# raise out of the whole pass — which took the other two kinds with it AND
# skipped the audit row, leaving a log indistinguishable from "extraction
# never ran". That is the exact blind spot this work exists to remove, so the
# malformed shapes must still produce a row.
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,reason",
    [
        ({"decisions": "Cut burn"}, "not_a_list"),
        ({"decisions": 7}, "not_a_list"),
        ({"decisions": ["Cut burn to 400k"]}, "item_not_a_dict"),
        ({"decisions": [None]}, "item_not_a_dict"),
    ],
    ids=["str", "int", "list-of-str", "list-of-null"],
)
async def test_malformed_items_are_dropped_not_raised(
    payload: dict[str, Any], reason: str, db: Path
) -> None:
    stores, audit = await _run_pass(payload, db)

    assert [s.call_count for s in stores] == [0, 0, 0]
    assert len(audit) == 1, "the pass must still audit itself"
    assert audit[0]["details"]["failure"] == "", "must not have raised"
    assert audit[0]["details"]["dropped"] == [
        {"kind": "decision", "reason": reason}
    ], "the kind must match the singular spelling used by per-item drops"


@pytest.mark.asyncio
async def test_a_null_kind_is_not_a_drop(db: Path) -> None:
    """The model omitting a kind is normal, not malformed."""
    _, audit = await _run_pass(
        db_path=db, payload={"decisions": None, "initiatives": []}
    )

    assert audit[0]["details"]["dropped"] == []
    assert audit[0]["details"]["proposed"] == {
        "decisions": 0, "initiatives": 0, "advice": 0
    }


@pytest.mark.asyncio
async def test_one_malformed_kind_does_not_lose_the_others(db: Path) -> None:
    """A bad `decisions` value used to abort the pass before `initiatives`
    was ever read, silently discarding good items alongside the bad one."""
    stores, audit = await _run_pass(db_path=db, payload={
        "decisions": "oops",
        "initiatives": [
            {"title": "Cut burn", "summary": "Down to 400k",
             "user_commitment_quote": "Cut burn to 400k"},
        ],
    })

    assert stores.initiative.call_args.kwargs == {
        "title": "Cut burn",
        "status": "active",
        "summary": "Down to 400k",
        "db_path": db,
        # No principal on this roster, so the update credits nobody.
        "updated_by_person_id": None,
    }
    assert audit[0]["details"]["stored"]["initiatives"] == 1
    assert audit[0]["details"]["dropped"] == [
        {"kind": "decision", "reason": "not_a_list"}
    ]


@pytest.mark.asyncio
async def test_a_non_dict_payload_is_dropped(db: Path) -> None:
    stores, audit = await _run_pass(["decisions"], db)

    assert [s.call_count for s in stores] == [0, 0, 0]
    assert audit[0]["details"]["dropped"] == [
        {"kind": "pass", "reason": "payload_not_a_dict"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"decisions": [{"summary": "", "user_commitment_quote": "Cut burn to 400k"}]},
         {"kind": "decision", "reason": "missing_field", "field": "summary"}),
        ({"initiatives": [{"summary": "Down to 400k",
                           "user_commitment_quote": "Cut burn to 400k"}]},
         {"kind": "initiative", "reason": "missing_field", "field": "title"}),
        ({"advice": [{"query_summary": "How much runway?",
                      "user_commitment_quote": "Cut burn to 400k"}]},
         {"kind": "advice", "reason": "missing_field", "field": "advice_summary"}),
    ],
    ids=["decision-summary", "initiative-title", "advice-summary"],
)
async def test_an_incomplete_item_is_recorded_as_a_drop(
    payload: dict[str, Any], expected: dict[str, str], db: Path
) -> None:
    """These paths counted the item as proposed and then `continue`d without a
    drop record, so `proposed == stored + dropped` did not hold and the audit
    row understated how much the model had thrown away."""
    _, audit = await _run_pass(payload, db)

    assert audit[0]["details"]["dropped"] == [expected]


@pytest.mark.asyncio
async def test_proposed_equals_stored_plus_dropped(db: Path) -> None:
    """The arithmetic an operator reads the row for. One item of every
    outcome: stored, bad quote, incomplete, and a malformed non-dict."""
    _, audit = await _run_pass(db_path=db, payload={
        "decisions": [
            {"summary": "Cut burn to 400k", "user_commitment_quote": "Cut burn to 400k"},
            {"summary": "Sell the building", "user_commitment_quote": "sell the building"},
            {"summary": "", "user_commitment_quote": "Cut burn to 400k"},
            "not a dict",
        ],
    }, retry_payload={})  # the bad quote triggers a retry; it re-quotes nothing

    details = audit[0]["details"]
    assert details["proposed"]["decisions"] == 3, "the non-dict never becomes an item"
    assert details["stored"]["decisions"] == 1
    assert details["dropped_count"] == 3, "two rejected items plus the non-dict"

    # A malformed shape is a drop that was never a proposed item, so it is
    # excluded here. Every item that WAS proposed must be stored or dropped —
    # a path that counts one and then falls through silently is the defect.
    shape_reasons = {"not_a_list", "item_not_a_dict", "payload_not_a_dict"}
    item_drops = [d for d in details["dropped"] if d["reason"] not in shape_reasons]
    assert sum(details["proposed"].values()) == (
        sum(details["stored"].values()) + len(item_drops)
    )
    assert sorted(d["reason"] for d in details["dropped"]) == [
        "bad_quote", "item_not_a_dict", "missing_field",
    ]


@pytest.mark.asyncio
async def test_a_crashed_pass_still_audits_with_a_failure_reason(db: Path) -> None:
    """A provider outage that writes no row at all reads exactly like
    "extraction was never scheduled" — the state this row exists to rule out.
    """
    rows: list[dict[str, Any]] = []

    def _capture(event_type: str, summary: str, **kw: Any) -> None:
        rows.append({"event_type": event_type, "summary": summary, **kw})

    with (
        mock.patch("openexecutive.audit.log_event", _capture),
        mock.patch("openexecutive.config.get_settings"),
        mock.patch.object(episodic, "get_active_initiatives", return_value=[]),
        mock.patch("openexecutive.providers.get_provider") as provider,
    ):
        provider.return_value.messages_create = mock.AsyncMock(
            side_effect=RuntimeError("upstream 529")
        )
        await episodic.extract_and_store(
            "Cut burn to 400k.", "ok", db_path=db, session_id="s-1"
        )

    audit = [r for r in rows if r["event_type"] == "memory_extraction"]
    assert len(audit) == 1
    assert audit[0]["details"]["failure"] == "RuntimeError"
    assert audit[0]["summary"].startswith("FAILED(RuntimeError)")


# --------------------------------------------------------------------- #
# Field types
#
# `_iter_items` guards the container shapes; it says nothing about what is
# inside an item. A non-string `user_commitment_quote` or label used to raise
# out of the whole pass from inside the validator, which lost the OTHER kinds
# in the same payload — the regression the malformed-container tests above
# claim to pin, arriving through a door they do not cover.
# --------------------------------------------------------------------- #


_VALID_INITIATIVE = {
    "title": "Cut burn",
    "summary": "Down to 400k",
    "user_commitment_quote": "Cut burn to 400k",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        {"summary": "S", "user_commitment_quote": 5},
        {"summary": 123, "user_commitment_quote": "nope"},
        {"summary": ["a"], "user_commitment_quote": None},
    ],
    ids=["int-quote", "int-summary", "list-summary"],
)
async def test_non_string_fields_do_not_abort_the_pass(
    decision: dict[str, Any], db: Path
) -> None:
    stores, audit = await _run_pass(
        db_path=db,
        payload={"decisions": [decision], "initiatives": [_VALID_INITIATIVE]},
        retry_payload={},  # a rejected quote triggers a retry; it re-quotes nothing
    )

    assert audit[0]["details"]["failure"] == "", "the pass must not have raised"
    assert stores.initiative.call_count == 1, (
        "a bad decision must not cost the valid initiative beside it"
    )
    assert stores.decision.call_count == 0


@pytest.mark.asyncio
async def test_a_failed_write_is_not_counted_as_stored(db: Path) -> None:
    """`stored` counted the item before the INSERT, so a locked database
    reported `stored>0` while writing nothing.

    That inverts the one alarm this row exists for: a total store-layer
    failure would surface as a healthy-looking pass instead of the
    `proposed>0, stored=0` streak the operator is told to watch for.
    """
    class _Block:
        type = "tool_use"
        name = "store_memories"
        input = {"decisions": [
            {"summary": "Cut burn to 400k", "user_commitment_quote": "Cut burn to 400k"},
        ]}

    class _Response:
        content = [_Block()]

    rows: list[dict[str, Any]] = []

    with (
        mock.patch.object(
            episodic, "store_decision",
            side_effect=sqlite3.OperationalError("database is locked"),
        ),
        mock.patch(
            "openexecutive.audit.log_event",
            lambda event_type, summary, **kw: rows.append(
                {"event_type": event_type, "summary": summary, **kw}
            ),
        ),
        mock.patch("openexecutive.audit.usage.log_model_usage"),
        mock.patch("openexecutive.config.get_settings"),
        mock.patch.object(episodic, "get_active_initiatives", return_value=[]),
        mock.patch("openexecutive.providers.get_provider") as provider,
    ):
        provider.return_value.messages_create = mock.AsyncMock(
            return_value=_Response()
        )
        await episodic.extract_and_store(
            "Cut burn to 400k.", "ok", db_path=db, session_id="s-1"
        )

    audit = [r for r in rows if r["event_type"] == "memory_extraction"]
    assert audit[0]["details"]["stored"]["decisions"] == 0, (
        "nothing was written, so nothing may be reported as stored"
    )
    assert audit[0]["details"]["failure"] == "OperationalError"


@pytest.mark.asyncio
async def test_a_cancelled_pass_is_not_logged_as_an_empty_one(db: Path) -> None:
    """`CancelledError` is a `BaseException`, so `except Exception` misses it
    while the `finally` still writes a row.

    The row then read `proposed=0 stored=0 failure=''` — byte for byte what
    "the model proposed nothing" looks like, which is the exact ambiguity the
    row was added to remove. Cancellation must still propagate.
    """
    rows: list[dict[str, Any]] = []

    async def _hang(*args: Any, **kwargs: Any) -> Any:
        # Long enough to still be awaiting when the cancel lands, short enough
        # that a cancel which does NOT land fails this test in seconds rather
        # than hanging the CI job until its own timeout.
        await asyncio.sleep(5)
        raise AssertionError("the pass was never cancelled")

    with (
        mock.patch(
            "openexecutive.audit.log_event",
            lambda event_type, summary, **kw: rows.append(
                {"event_type": event_type, "summary": summary, **kw}
            ),
        ),
        mock.patch("openexecutive.config.get_settings"),
        mock.patch.object(episodic, "get_active_initiatives", return_value=[]),
        mock.patch("openexecutive.providers.get_provider") as provider,
    ):
        provider.return_value.messages_create = _hang
        task = asyncio.create_task(
            episodic.extract_and_store(
                "Cut burn to 400k.", "ok", db_path=db, session_id="s-1"
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    audit = [r for r in rows if r["event_type"] == "memory_extraction"]
    assert len(audit) == 1
    assert audit[0]["details"]["failure"] == "CancelledError"
    assert audit[0]["summary"].startswith("FAILED(CancelledError)")


@pytest.mark.asyncio
async def test_a_garbage_pass_does_not_read_as_an_empty_one(db: Path) -> None:
    """`proposed` counts only items the model emitted as objects, so a payload
    that was nothing but garbage would otherwise read `proposed=0 stored=0` —
    identical to a turn with nothing to extract. `malformed` separates them."""
    _, audit = await _run_pass(db_path=db, payload={"decisions": ["a", "b"]})

    details = audit[0]["details"]
    assert details["proposed"]["decisions"] == 0
    assert details["malformed_count"] == 2
    assert "malformed=2" in audit[0]["summary"]

    _, quiet = await _run_pass(db_path=db, payload={"decisions": []})
    assert quiet[0]["details"]["malformed_count"] == 0


# --------------------------------------------------------------------- #
# The corrective retry
#
# The validator rejects a paraphrase, and the model paraphrases: on a live
# install two of three real commitments were lost that way — the quote it
# gave began "SVN's engagement covers only ..." where the user had typed
# something else. One more call, only on turns that dropped a bad quote,
# shows the model exactly what failed and asks for the verbatim span.
# --------------------------------------------------------------------- #

_USER = "therea re only 2 properties in SVN scope. Tell the team."
_BAD = {
    "domain": "operations",
    "summary": "SVN scope is two properties",
    "user_commitment_quote": "There are only two properties in SVN's scope",
}
_GOOD = {**_BAD, "user_commitment_quote": "therea re only 2 properties in SVN scope"}


@pytest.mark.asyncio
async def test_a_bad_quote_is_retried_once_and_recovered(db: Path) -> None:
    result = await _run_pass_full(
        {"decisions": [_BAD]}, db, user_message=_USER,
        retry_payload={"decisions": [_GOOD]},
    )

    assert result.create.await_count == 2, "exactly one corrective call"
    assert result.stores.decision.call_count == 1
    assert result.stores.decision.call_args.kwargs["summary"] == _BAD["summary"]
    details = result.audit[0]["details"]
    assert details["proposed"]["decisions"] == 1, "a recovery is not a new proposal"
    assert details["stored"]["decisions"] == 1
    assert details["dropped"] == [], "the recovered item's drop record is released"
    assert details["retry"] == {**_NO_RETRY, "rejected": 1, "recovered": 1}
    assert result.audit[0]["summary"].endswith("retry=1/1")


@pytest.mark.asyncio
async def test_the_retry_prompt_names_the_rejected_item_and_forces_the_tool(
    db: Path,
) -> None:
    result = await _run_pass_full(
        {"decisions": [_BAD]}, db, user_message=_USER, retry_payload={},
    )

    first, second = result.create.call_args_list
    content = second.kwargs["messages"][0]["content"]
    assert content.startswith(first.kwargs["messages"][0]["content"]), (
        "the retry sees the same turn the first pass saw"
    )
    assert "REJECTED ITEMS" in content
    assert _BAD["summary"] in content and _BAD["user_commitment_quote"] in content
    assert "character-for-character" in content
    assert second.kwargs["tool_choice"] == {"type": "tool", "name": "store_memories"}
    assert second.kwargs["system"] == first.kwargs["system"]


@pytest.mark.asyncio
async def test_no_retry_without_a_bad_quote_drop(db: Path) -> None:
    """A missing field is not something a second call can fix."""
    result = await _run_pass_full(
        {"decisions": [{"summary": "", "user_commitment_quote": "Cut burn to 400k"}]},
        db,
    )

    assert result.create.await_count == 1
    assert result.audit[0]["details"]["retry"] == _NO_RETRY
    assert "retry=" not in result.audit[0]["summary"]


@pytest.mark.asyncio
async def test_a_retry_that_still_fails_keeps_the_original_drop(db: Path) -> None:
    result = await _run_pass_full(
        {"decisions": [_BAD]}, db, user_message=_USER,
        retry_payload={"decisions": [_BAD]},
    )

    details = result.audit[0]["details"]
    assert details["dropped"] == [
        {"kind": "decision", "reason": "bad_quote", "label": _BAD["summary"]}
    ], "still invalid: one record, not two"
    assert details["stored"]["decisions"] == 0
    assert details["retry"] == {**_NO_RETRY, "rejected": 1, "still_invalid": 1}, (
        "the second bad quote is counted, not filed as a second drop"
    )
    assert sum(details["proposed"].values()) == (
        sum(details["stored"].values()) + len(details["dropped"])
    )


@pytest.mark.asyncio
async def test_extra_retry_items_count_as_new_proposals(db: Path) -> None:
    """Told to add nothing, the model may still add; a valid extra item is
    real, so it is stored and counted as proposed — never as a recovery, in
    whichever order it arrives."""
    extra = {"summary": "Tell the team", "user_commitment_quote": "Tell the team"}
    result = await _run_pass_full(
        {"decisions": [_BAD]}, db, user_message=_USER,
        retry_payload={"decisions": [extra, _GOOD]},
    )

    details = result.audit[0]["details"]
    assert details["proposed"]["decisions"] == 2
    assert details["stored"]["decisions"] == 2
    assert details["dropped"] == []
    assert details["retry"]["recovered"] == 1


@pytest.mark.asyncio
async def test_a_re_submitted_stored_item_is_not_stored_twice(db: Path) -> None:
    """The model re-sending everything must not double-count `stored`."""
    valid = {"summary": "Tell the team", "user_commitment_quote": "Tell the team"}
    result = await _run_pass_full(
        {"decisions": [valid, _BAD]}, db, user_message=_USER,
        retry_payload={"decisions": [valid, _GOOD]},
    )

    details = result.audit[0]["details"]
    assert result.stores.decision.call_count == 2, "valid once, recovered once"
    assert details["proposed"]["decisions"] == 2
    assert details["stored"]["decisions"] == 2
    assert details["retry"] == {**_NO_RETRY, "rejected": 1, "recovered": 1, "repeated": 1}


@pytest.mark.asyncio
async def test_a_recovery_of_one_kind_does_not_release_another(db: Path) -> None:
    result = await _run_pass_full(
        {"decisions": [_BAD]}, db, user_message=_USER,
        retry_payload={"initiatives": [{
            "title": "SVN scope", "summary": "Two properties",
            "user_commitment_quote": "therea re only 2 properties in SVN scope",
        }]},
    )

    details = result.audit[0]["details"]
    assert details["dropped"] == [
        {"kind": "decision", "reason": "bad_quote", "label": _BAD["summary"]}
    ]
    assert details["proposed"] == {"decisions": 1, "initiatives": 1, "advice": 0}
    assert details["stored"] == {"decisions": 0, "initiatives": 1, "advice": 0}
    assert details["retry"]["recovered"] == 0


@pytest.mark.asyncio
async def test_a_retry_failure_keeps_the_first_pass_results(db: Path) -> None:
    valid = {"summary": "Tell the team", "user_commitment_quote": "Tell the team"}
    result = await _run_pass_full(
        {"decisions": [valid, _BAD]}, db, user_message=_USER,
        retry_payload=RuntimeError("upstream 529"),
    )

    details = result.audit[0]["details"]
    assert details["failure"] == "", "the pass itself did not fail"
    assert not result.audit[0]["summary"].startswith("FAILED")
    assert details["stored"]["decisions"] == 1
    assert details["dropped"] == [
        {"kind": "decision", "reason": "bad_quote", "label": _BAD["summary"]}
    ]
    assert details["retry"] == {**_NO_RETRY, "rejected": 1, "failure": "RuntimeError"}


@pytest.mark.asyncio
async def test_a_cancelled_retry_is_logged_as_failed(db: Path) -> None:
    """Cancellation is not the retry's to swallow: the row must say FAILED,
    and its `retry` block must still say a corrective pass was owed."""
    rows: list[dict[str, Any]] = []

    def _capture(event_type: str, summary: str, **kw: Any) -> None:
        rows.append({"event_type": event_type, "summary": summary, **kw})

    with (
        mock.patch.object(episodic, "store_decision"),
        mock.patch("openexecutive.audit.log_event", _capture),
        mock.patch("openexecutive.audit.usage.log_model_usage"),
        mock.patch("openexecutive.config.get_settings"),
        mock.patch.object(episodic, "get_active_initiatives", return_value=[]),
        mock.patch("openexecutive.providers.get_provider") as provider,
    ):
        provider.return_value.messages_create = mock.AsyncMock(
            side_effect=[_tool_response({"decisions": [_BAD]}), asyncio.CancelledError()]
        )
        with pytest.raises(asyncio.CancelledError):
            await episodic.extract_and_store(_USER, "ok", db_path=db, session_id="s-1")

    audit = [r for r in rows if r["event_type"] == "memory_extraction"]
    assert audit[0]["summary"].startswith("FAILED(CancelledError)")
    assert audit[0]["details"]["dropped"] == [
        {"kind": "decision", "reason": "bad_quote", "label": _BAD["summary"]}
    ]
    assert audit[0]["details"]["retry"] == {**_NO_RETRY, "rejected": 1}, (
        "a zeroed block would deny the retry that the drop record proves was owed"
    )


@pytest.mark.asyncio
async def test_no_retry_when_the_first_pass_failed(db: Path) -> None:
    rows: list[dict[str, Any]] = []

    def _capture(event_type: str, summary: str, **kw: Any) -> None:
        rows.append({"event_type": event_type, "summary": summary, **kw})

    with (
        mock.patch("openexecutive.audit.log_event", _capture),
        mock.patch("openexecutive.config.get_settings"),
        mock.patch.object(episodic, "get_active_initiatives", return_value=[]),
        mock.patch("openexecutive.providers.get_provider") as provider,
    ):
        create = mock.AsyncMock(side_effect=RuntimeError("upstream 529"))
        provider.return_value.messages_create = create
        await episodic.extract_and_store(_USER, "ok", db_path=db, session_id="s-1")

    assert create.await_count == 1
    audit = [r for r in rows if r["event_type"] == "memory_extraction"]
    assert audit[0]["details"]["failure"] == "RuntimeError"
    assert audit[0]["details"]["retry"] == _NO_RETRY


@pytest.mark.asyncio
async def test_a_reworded_re_submission_is_a_new_proposal_and_the_drop_stands(
    db: Path,
) -> None:
    """A valid item that matches no drop record by label is not a recovery,
    even if the model meant it as one — "reworded" and "different" cannot be
    told apart, and crediting it would erase the bad-quote census."""
    reworded = {**_GOOD, "summary": "Only two properties are in SVN's scope"}
    result = await _run_pass_full(
        {"decisions": [_BAD]}, db, user_message=_USER,
        retry_payload={"decisions": [reworded]},
    )

    details = result.audit[0]["details"]
    assert result.stores.decision.call_count == 1, "the valid item is still kept"
    assert details["proposed"]["decisions"] == 2
    assert details["stored"]["decisions"] == 1
    assert details["dropped"] == [
        {"kind": "decision", "reason": "bad_quote", "label": _BAD["summary"]}
    ]
    assert details["retry"]["recovered"] == 0


@pytest.mark.asyncio
async def test_a_re_sent_stored_item_with_a_reworded_label_is_repeated(
    db: Path,
) -> None:
    """The model rewords labels but keeps a quote that passed, so the quote
    is what identifies a re-send. Same quote, new label: stored once."""
    valid = {"summary": "Tell the team about it", "user_commitment_quote": "Tell the team"}
    reworded = {**valid, "summary": "Tell the whole team about it"}
    result = await _run_pass_full(
        {"decisions": [valid, _BAD]}, db, user_message=_USER,
        retry_payload={"decisions": [reworded, _GOOD]},
    )

    details = result.audit[0]["details"]
    assert result.stores.decision.call_count == 2, "valid once, recovered once"
    assert details["stored"]["decisions"] == 2
    assert details["retry"] == {**_NO_RETRY, "rejected": 1, "recovered": 1, "repeated": 1}


@pytest.mark.asyncio
async def test_a_corrected_item_sharing_a_stored_label_is_still_recovered(
    db: Path,
) -> None:
    """Two items with the same summary, one stored and one rejected: the
    corrected one carries a different quote, so it is not a re-send."""
    stored_twin = {**_BAD, "user_commitment_quote": "Tell the team"}
    result = await _run_pass_full(
        {"decisions": [stored_twin, _BAD]}, db, user_message=_USER,
        retry_payload={"decisions": [_GOOD]},
    )

    details = result.audit[0]["details"]
    assert result.stores.decision.call_count == 2
    assert details["dropped"] == []
    assert details["retry"] == {**_NO_RETRY, "rejected": 1, "recovered": 1}


@pytest.mark.asyncio
async def test_retry_garbage_is_counted_not_hidden(db: Path) -> None:
    """A retry that returned only garbage must not read like one that
    obediently returned empty arrays — the ambiguity the row exists to remove."""
    result = await _run_pass_full(
        {"decisions": [_BAD]}, db, user_message=_USER,
        retry_payload={"decisions": "nope", "advice": ["x"], "initiatives": [_BAD]},
    )

    details = result.audit[0]["details"]
    assert details["malformed_count"] == 0, "the first pass was clean"
    assert details["dropped"] == [
        {"kind": "decision", "reason": "bad_quote", "label": _BAD["summary"]}
    ], "retry drops never become first-pass drop records"
    assert details["retry"] == {
        **_NO_RETRY, "rejected": 1, "malformed": 2, "still_invalid": 1,
    }


@pytest.mark.asyncio
async def test_a_correctly_requoted_item_sharing_a_stored_sentence_is_recovered(
    db: Path,
) -> None:
    """Two commitments can rest on one sentence. A re-submission whose label
    matches a pending drop is a recovery even though its (now correct) quote
    already supports a stored item — the drop record is checked first."""
    first = {"summary": "Cut burn to 400k", "user_commitment_quote": "Cut burn to 400k"}
    freeze_bad = {"summary": "Freeze hiring", "user_commitment_quote": "We will freeze hiring"}
    freeze_good = {**freeze_bad, "user_commitment_quote": "Cut burn to 400k"}
    result = await _run_pass_full(
        {"decisions": [first, freeze_bad]}, db, user_message="Cut burn to 400k.",
        retry_payload={"decisions": [freeze_good]},
    )

    details = result.audit[0]["details"]
    assert [c.kwargs["summary"] for c in result.stores.decision.call_args_list] == [
        "Cut burn to 400k", "Freeze hiring",
    ]
    assert details["dropped"] == []
    assert details["retry"] == {**_NO_RETRY, "rejected": 1, "recovered": 1}


@pytest.mark.asyncio
async def test_a_re_sent_item_whose_quote_gained_a_full_stop_is_repeated(
    db: Path,
) -> None:
    """Asked to re-copy character-for-character, the model may add or drop the
    terminator; that must not mint a new identity and a second `stored`."""
    valid = {"summary": "Tell the team", "user_commitment_quote": "Tell the team"}
    result = await _run_pass_full(
        {"decisions": [valid, _BAD]}, db, user_message=_USER,
        retry_payload={"decisions": [
            {**valid, "user_commitment_quote": "Tell the team."}, _GOOD,
        ]},
    )

    details = result.audit[0]["details"]
    assert result.stores.decision.call_count == 2, "valid once, recovered once"
    assert details["proposed"]["decisions"] == 2
    assert details["retry"] == {**_NO_RETRY, "rejected": 1, "recovered": 1, "repeated": 1}


@pytest.mark.asyncio
async def test_an_invalid_re_send_is_still_invalid_not_repeated(db: Path) -> None:
    """Validation comes first: an item with a stored quote but no summary was
    never a re-send of anything."""
    valid = {"summary": "Tell the team", "user_commitment_quote": "Tell the team"}
    result = await _run_pass_full(
        {"decisions": [valid, _BAD]}, db, user_message=_USER,
        retry_payload={"decisions": [{"summary": "", "user_commitment_quote": "Tell the team"}]},
    )

    assert result.audit[0]["details"]["retry"] == {
        **_NO_RETRY, "rejected": 1, "still_invalid": 1,
    }


class _NoToolCall:
    """A retry response with no `store_memories` block at all."""
    content: list[Any] = []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retry_payload",
    [["decisions"], _NoToolCall()],
    ids=["payload-not-a-dict", "no-tool-call"],
)
async def test_retry_garbage_above_item_level_is_counted(
    retry_payload: Any, db: Path
) -> None:
    """A retry that returned a non-dict payload, or no `store_memories` call at
    all, must not read like one that obediently returned empty arrays."""
    result = await _run_pass_full(
        {"decisions": [_BAD]}, db, user_message=_USER, retry_payload=retry_payload,
    )

    assert result.audit[0]["details"]["retry"] == {
        **_NO_RETRY, "rejected": 1, "malformed": 1,
    }


@pytest.mark.asyncio
async def test_a_store_failure_before_the_retry_still_reports_what_was_owed(
    db: Path,
) -> None:
    """`rejected` is the count of drops a corrective pass was owed, whether or
    not the pass lived long enough to run it."""
    rows: list[dict[str, Any]] = []

    def _capture(event_type: str, summary: str, **kw: Any) -> None:
        rows.append({"event_type": event_type, "summary": summary, **kw})

    valid = {"summary": "Tell the team", "user_commitment_quote": "Tell the team"}
    with (
        mock.patch.object(
            episodic, "store_decision", side_effect=sqlite3.OperationalError("locked")
        ),
        mock.patch("openexecutive.audit.log_event", _capture),
        mock.patch("openexecutive.audit.usage.log_model_usage"),
        mock.patch("openexecutive.config.get_settings"),
        mock.patch.object(episodic, "get_active_initiatives", return_value=[]),
        mock.patch("openexecutive.providers.get_provider") as provider,
    ):
        create = mock.AsyncMock(return_value=_tool_response({"decisions": [_BAD, valid]}))
        provider.return_value.messages_create = create
        await episodic.extract_and_store(_USER, "ok", db_path=db, session_id="s-1")

    assert create.await_count == 1, "no retry after the pass failed"
    audit = [r for r in rows if r["event_type"] == "memory_extraction"]
    assert audit[0]["details"]["failure"] == "OperationalError"
    assert audit[0]["details"]["retry"] == {**_NO_RETRY, "rejected": 1}


@pytest.mark.asyncio
async def test_a_null_quote_is_not_the_word_none(db: Path) -> None:
    """`str(None)` is "None", which a user message can contain."""
    _, audit = await _run_pass(
        {"decisions": [{"summary": "Nothing is sold", "user_commitment_quote": None}]},
        db, user_message="None of them are sold.", retry_payload={},
    )

    assert audit[0]["details"]["stored"]["decisions"] == 0
    assert audit[0]["details"]["dropped"] == [
        {"kind": "decision", "reason": "bad_quote", "label": "Nothing is sold"}
    ]


def test_the_retry_prompt_shows_a_null_quote_as_empty() -> None:
    """The model wrote JSON null, not the word "None"; the prompt must not
    tell it otherwise."""
    prompt = episodic._retry_prompt(
        "USER QUESTION:\nx", [(episodic._DECISIONS, {"summary": "X", "user_commitment_quote": None})]
    )
    assert '- decision "X": quote given ""' in prompt
    assert "None" not in prompt
