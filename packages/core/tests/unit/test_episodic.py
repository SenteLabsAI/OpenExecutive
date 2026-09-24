"""Unit tests for episodic memory CRUD helpers."""
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.memory.episodic import (
    delete_advice,
    delete_decision,
    delete_initiative,
    format_for_prompt,
    get_advice,
    get_decision,
    get_initiative,
    get_recent_advice,
    get_recent_decisions,
    initialize_db,
    list_advice,
    list_decisions,
    list_initiatives,
    store_advice,
    store_decision,
    store_initiative,
    update_advice,
    update_decision,
    update_initiative,
)


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    return db_path


@pytest.fixture(autouse=True)
def _no_audit_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `memory_extraction` rows out of the default `./episodic_memory.db`.

    `_audit_extraction` calls `openexecutive.audit.log_event`, which targets
    the default DB regardless of the `db_path` the extraction was given. The
    tests below that drive `extract_and_store` therefore leak rows that other
    modules read back as real data — visible only in a full-suite run, which
    is the audit-log pollution trap in CLAUDE.md.
    """
    monkeypatch.setattr(
        "openexecutive.audit.log_event", lambda *a, **kw: None, raising=False
    )


def test_decision_update_and_delete(db: Path) -> None:
    store_decision("strategy", "Expand to EU", rationale="Larger market", db_path=db)
    items = list_decisions(db_path=db)
    assert len(items) == 1
    decision_id = items[0].id
    assert decision_id is not None

    ok = update_decision(decision_id, summary="Expand to Germany only", outcome="approved", db_path=db)
    assert ok is True
    refreshed = get_decision(decision_id, db_path=db)
    assert refreshed is not None
    assert refreshed.summary == "Expand to Germany only"
    assert refreshed.outcome == "approved"
    assert refreshed.rationale == "Larger market"

    assert delete_decision(decision_id, db_path=db) is True
    assert list_decisions(db_path=db) == []


def test_initiative_update_bumps_timestamp(db: Path) -> None:
    store_initiative("Hire CFO", "active", "Find a CFO by Q2", db_path=db)
    [initiative] = list_initiatives(db_path=db)
    assert initiative.id is not None
    initial_updated = initiative.updated_at

    ok = update_initiative(initiative.id, status="completed", db_path=db)
    assert ok is True
    refreshed = get_initiative(initiative.id, db_path=db)
    assert refreshed is not None
    assert refreshed.status == "completed"
    assert refreshed.updated_at >= initial_updated


def test_advice_crud(db: Path) -> None:
    store_advice("finance", "Should we raise?", "Yes, raise in Q3.", db_path=db)
    [advice] = list_advice(db_path=db)
    assert advice.id is not None

    ok = update_advice(advice.id, advice_summary="Raise in Q4 instead.", db_path=db)
    assert ok is True
    refreshed = get_advice(advice.id, db_path=db)
    assert refreshed is not None
    assert refreshed.advice_summary == "Raise in Q4 instead."

    assert delete_advice(advice.id, db_path=db) is True
    assert list_advice(db_path=db) == []


def test_format_for_prompt_empty_when_no_data(db: Path) -> None:
    assert format_for_prompt(db_path=db) == ""


def test_format_for_prompt_renders_decisions_and_initiatives(db: Path) -> None:
    store_decision("strategy", "Expand to EU", rationale="Larger market", db_path=db)
    store_initiative("Hire CFO", "active", "Find a CFO by Q2", db_path=db)

    rendered = format_for_prompt(db_path=db)
    assert "Recent decisions:" in rendered
    assert "Expand to EU" in rendered
    assert "[strategy]" in rendered
    assert "Active initiatives:" in rendered
    assert "Hire CFO" in rendered


def test_format_for_prompt_trims_oldest_decisions_when_over_max_chars(db: Path) -> None:
    # Store 8 decisions; each ~150 chars in rendered form. Cap at 400 chars to
    # force trimming. Oldest must be dropped first (newest-first semantics).
    for n in range(8):
        store_decision(
            domain="strategy",
            summary=f"Decision number {n} with some padding text " + ("x" * 80),
            rationale="r",
            db_path=db,
        )
    store_initiative("Keep me", "active", "stays even when decisions trimmed", db_path=db)

    rendered = format_for_prompt(db_path=db, max_chars=400)
    assert len(rendered) <= 400
    # Initiatives are preserved through trimming.
    assert "Keep me" in rendered
    # Newest decision (index 7) survives; oldest (index 0) should be dropped.
    assert "Decision number 7" in rendered
    assert "Decision number 0" not in rendered


def test_format_for_prompt_default_max_chars_is_generous(db: Path) -> None:
    """Existing callers (Executive user turn) should be unaffected by trimming."""
    for n in range(5):
        store_decision("strategy", f"Short decision {n}", db_path=db)
    rendered = format_for_prompt(db_path=db)  # default max_chars
    for n in range(5):
        assert f"Short decision {n}" in rendered


def _backdate_decision(db: Path, decision_id: int, days_ago: int) -> None:
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE decisions SET timestamp = ? WHERE id = ?", (ts, decision_id))


def _backdate_advice(db: Path, advice_id: int, days_ago: int) -> None:
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE advice_given SET timestamp = ? WHERE id = ?", (ts, advice_id))


def test_decision_dedup_within_seven_days(db: Path) -> None:
    """Re-storing the same decision (domain + 80-char summary prefix) within 7d should be a no-op."""
    store_decision("strategy", "Expand to EU next year", db_path=db)
    store_decision("strategy", "Expand to EU next year", db_path=db)
    assert len(list_decisions(db_path=db)) == 1

    # Different domain → not a dup, even with the same summary.
    store_decision("operations", "Expand to EU next year", db_path=db)
    assert len(list_decisions(db_path=db)) == 2

    # Different summary prefix → not a dup.
    store_decision("strategy", "Different decision entirely", db_path=db)
    assert len(list_decisions(db_path=db)) == 3


def test_decision_dedup_boundary_six_vs_eight_days(db: Path) -> None:
    """At 6d ago dedup still fires; at 8d ago it does not (window is 7d)."""
    store_decision("strategy", "Decision A", db_path=db)
    first_id = list_decisions(db_path=db)[0].id
    assert first_id is not None

    # Backdate to 6 days ago — within window, second store should dedup.
    _backdate_decision(db, first_id, days_ago=6)
    store_decision("strategy", "Decision A", db_path=db)
    assert len(list_decisions(db_path=db)) == 1

    # Backdate to 8 days ago — outside window, second store should land.
    _backdate_decision(db, first_id, days_ago=8)
    store_decision("strategy", "Decision A", db_path=db)
    assert len(list_decisions(db_path=db)) == 2


def test_decision_dedup_fills_missing_rationale(db: Path) -> None:
    """When a dup hits and the prior row had no rationale, the new rationale lands."""
    store_decision("strategy", "Launch in EU", rationale="", db_path=db)
    [first] = list_decisions(db_path=db)
    assert first.rationale == ""
    assert first.id is not None

    store_decision("strategy", "Launch in EU", rationale="Larger TAM", db_path=db)
    assert len(list_decisions(db_path=db)) == 1
    refreshed = get_decision(first.id, db_path=db)
    assert refreshed is not None
    assert refreshed.rationale == "Larger TAM"

    # A second dup with a different rationale should NOT overwrite the existing one.
    store_decision("strategy", "Launch in EU", rationale="Some other reason", db_path=db)
    refreshed = get_decision(first.id, db_path=db)
    assert refreshed is not None
    assert refreshed.rationale == "Larger TAM"


def test_advice_dedup_within_seven_days(db: Path) -> None:
    """Re-storing the same advice (domain + advice_summary prefix) within 7d should be a no-op."""
    store_advice("finance", "Should we raise?", "Yes, raise a Series A in Q3.", db_path=db)
    store_advice("finance", "Different question phrasing?", "Yes, raise a Series A in Q3.", db_path=db)
    assert len(list_advice(db_path=db)) == 1

    # Different domain → not a dup.
    store_advice("strategy", "Should we raise?", "Yes, raise a Series A in Q3.", db_path=db)
    assert len(list_advice(db_path=db)) == 2

    # Different advice_summary → not a dup.
    store_advice("finance", "Should we raise?", "No, focus on revenue first.", db_path=db)
    assert len(list_advice(db_path=db)) == 3


def test_advice_dedup_boundary_six_vs_eight_days(db: Path) -> None:
    """Advice dedup also uses a 7d window."""
    store_advice("finance", "Q1", "Raise now.", db_path=db)
    first_id = list_advice(db_path=db)[0].id
    assert first_id is not None

    _backdate_advice(db, first_id, days_ago=6)
    store_advice("finance", "Q1", "Raise now.", db_path=db)
    assert len(list_advice(db_path=db)) == 1

    _backdate_advice(db, first_id, days_ago=8)
    store_advice("finance", "Q1", "Raise now.", db_path=db)
    assert len(list_advice(db_path=db)) == 2


def test_missing_id_returns_false(db: Path) -> None:
    assert update_decision(99999, summary="nope", db_path=db) is False
    assert update_initiative(99999, status="completed", db_path=db) is False
    assert update_advice(99999, advice_summary="nope", db_path=db) is False
    assert delete_decision(99999, db_path=db) is False
    assert delete_initiative(99999, db_path=db) is False
    assert delete_advice(99999, db_path=db) is False
    assert get_decision(99999, db_path=db) is None
    assert get_initiative(99999, db_path=db) is None
    assert get_advice(99999, db_path=db) is None


# --------------------------------------------------------------------------- #
# Session-scoped episodic context (Phase 5)
# --------------------------------------------------------------------------- #

def test_store_decision_persists_session_id(db: Path) -> None:
    store_decision("finance", "Extend runway", session_id="discord:thread:111", db_path=db)
    items = list_decisions(db_path=db)
    assert len(items) == 1
    assert items[0].session_id == "discord:thread:111"


def test_store_advice_persists_session_id(db: Path) -> None:
    store_advice("hr", "hiring freeze", "Pause all non-critical hiring", session_id="telegram:42", db_path=db)
    items = list_advice(db_path=db)
    assert len(items) == 1
    assert items[0].session_id == "telegram:42"


def test_get_recent_decisions_scoped_excludes_other_sessions(db: Path) -> None:
    store_decision("finance", "Cut costs", session_id="discord:thread:A", db_path=db)
    store_decision("strategy", "Enter EU", session_id="discord:thread:B", db_path=db)
    store_decision("ops", "Hire COO", session_id="", db_path=db)  # unscoped/global

    scoped = get_recent_decisions(db_path=db, session_id="discord:thread:A")
    assert len(scoped) == 1
    assert scoped[0].domain == "finance"


def test_get_recent_decisions_global_returns_all(db: Path) -> None:
    store_decision("finance", "Cut costs", session_id="discord:thread:A", db_path=db)
    store_decision("strategy", "Enter EU", session_id="discord:thread:B", db_path=db)

    all_decisions = get_recent_decisions(db_path=db, session_id="")
    assert len(all_decisions) == 2


def test_get_recent_advice_scoped_excludes_other_sessions(db: Path) -> None:
    store_advice("hr", "q1", "Prioritise culture", session_id="discord:thread:A", db_path=db)
    store_advice("legal", "q2", "Consult lawyer", session_id="discord:thread:B", db_path=db)

    scoped = get_recent_advice(db_path=db, session_id="discord:thread:A")
    assert len(scoped) == 1
    assert scoped[0].domain == "hr"


def test_format_for_prompt_scopes_decisions_and_advice_keeps_initiatives(db: Path) -> None:
    """Decisions and advice are scoped to the session; initiatives stay global."""
    store_decision("finance", "Thread A decision", session_id="discord:thread:A", db_path=db)
    store_decision("strategy", "Thread B decision", session_id="discord:thread:B", db_path=db)
    store_advice("hr", "Thread A advice q", "Thread A advice body", session_id="discord:thread:A", db_path=db)
    store_initiative("Company-wide initiative", "active", db_path=db)

    prompt = format_for_prompt(db_path=db, session_id="discord:thread:A")

    # Thread A's data is present
    assert "Thread A decision" in prompt
    assert "Thread A advice body" in prompt
    # Thread B's decision is excluded
    assert "Thread B decision" not in prompt
    # Initiative is always included (company-wide)
    assert "Company-wide initiative" in prompt


def test_format_for_prompt_empty_session_id_returns_all(db: Path) -> None:
    """Empty session_id → global mode: returns decisions from all sessions."""
    store_decision("finance", "Decision A", session_id="discord:thread:A", db_path=db)
    store_decision("strategy", "Decision B", session_id="discord:thread:B", db_path=db)

    prompt = format_for_prompt(db_path=db, session_id="")
    assert "Decision A" in prompt
    assert "Decision B" in prompt


def test_format_for_prompt_scoped_empty_when_no_session_data(db: Path) -> None:
    """A session with no extracted decisions/advice gets empty episodic context."""
    store_decision("finance", "Someone else's decision", session_id="discord:thread:other", db_path=db)

    prompt = format_for_prompt(db_path=db, session_id="discord:thread:mine")
    # No decisions for this session; initiatives empty too
    assert prompt == ""


def test_dedup_does_not_suppress_same_decision_across_sessions(db: Path) -> None:
    """Two sessions producing the same decision must each get their own row —
    dedup must not fire across session boundaries. Without this, thread B
    would silently lose its decision because thread A stored the same one first."""
    store_decision("finance", "Cut costs", session_id="discord:thread:A", db_path=db)
    store_decision("finance", "Cut costs", session_id="discord:thread:B", db_path=db)

    all_items = list_decisions(db_path=db)
    assert len(all_items) == 2
    session_ids = {d.session_id for d in all_items}
    assert session_ids == {"discord:thread:A", "discord:thread:B"}


def test_dedup_still_works_within_same_session(db: Path) -> None:
    """Dedup should still prevent the same decision from being stored twice in
    the same session within 7 days."""
    store_decision("finance", "Cut costs", session_id="discord:thread:A", db_path=db)
    store_decision("finance", "Cut costs", session_id="discord:thread:A", db_path=db)

    all_items = list_decisions(db_path=db)
    assert len(all_items) == 1


def test_initialize_db_migration_idempotent(db: Path) -> None:
    """Calling initialize_db a second time on an existing DB must not fail —
    the PRAGMA-guarded ALTERs are no-ops when the column already exists."""
    initialize_db(db)  # second call — should not raise
    # Verify session_id column exists on both tables
    conn = sqlite3.connect(str(db))
    try:
        decisions_cols = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
        advice_cols = {row[1] for row in conn.execute("PRAGMA table_info(advice_given)")}
    finally:
        conn.close()
    assert "session_id" in decisions_cols
    assert "session_id" in advice_cols


def test_extract_and_store_includes_existing_initiatives_in_user_turn(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extraction LLM must see currently-active initiatives so it reuses
    canonical titles instead of inventing new variants. Without this we
    accumulate many rows for the same real-world project under variant names."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from openexecutive.memory import episodic as ep

    store_initiative("AI Opportunity Assessment", "active", "x", db_path=db)
    store_initiative("Karel soft-launch", "active", "y", db_path=db)

    create_mock = AsyncMock(return_value=SimpleNamespace(content=[]))
    fake_provider = SimpleNamespace(messages_create=create_mock)
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: fake_provider)
    monkeypatch.setattr(
        "openexecutive.config.get_settings",
        lambda: SimpleNamespace(routing_model="claude-test"),
    )

    asyncio.run(ep.extract_and_store("user message", "assistant response", db_path=db))

    kwargs = create_mock.await_args.kwargs
    user_content = kwargs["messages"][0]["content"]
    assert "EXISTING ACTIVE INITIATIVES" in user_content
    assert "AI Opportunity Assessment" in user_content
    assert "Karel soft-launch" in user_content
    assert "USER QUESTION:\nuser message" in user_content


def test_extract_and_store_omits_block_when_no_active_initiatives(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no active initiatives, the block is omitted entirely — don't waste
    tokens on an empty header that would confuse the model."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from openexecutive.memory import episodic as ep

    create_mock = AsyncMock(return_value=SimpleNamespace(content=[]))
    fake_provider = SimpleNamespace(messages_create=create_mock)
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: fake_provider)
    monkeypatch.setattr(
        "openexecutive.config.get_settings",
        lambda: SimpleNamespace(routing_model="claude-test"),
    )

    asyncio.run(ep.extract_and_store("u", "a", db_path=db))

    kwargs = create_mock.await_args.kwargs
    user_content = kwargs["messages"][0]["content"]
    assert "EXISTING ACTIVE INITIATIVES" not in user_content
    assert user_content.startswith("USER QUESTION:")


def test_extraction_prompt_includes_source_attribution_rule() -> None:
    """The extraction LLM kept logging the executive's recommendations as user
    commitments because the prompt only said 'the user explicitly committed' —
    it never spelled out that prescriptive prose in the EXECUTIVE RESPONSE block
    is a proposal, not a commitment. If a future edit drops this clause, the
    over-logging regression returns. Guard the wording so refactors notice.
    """
    from openexecutive.memory.episodic import _EXTRACTION_SYSTEM, _EXTRACTION_TOOL

    # The system prompt must call out source attribution explicitly.
    assert "Source attribution" in _EXTRACTION_SYSTEM
    assert "USER QUESTION" in _EXTRACTION_SYSTEM
    assert "EXECUTIVE RESPONSE" in _EXTRACTION_SYSTEM
    assert "proposal" in _EXTRACTION_SYSTEM.lower()

    # The decisions/initiatives/advice schema descriptions must each restate
    # the USER-only attribution rule so the LLM sees it at field level too.
    tool_desc = _EXTRACTION_TOOL["description"]
    assert "USER QUESTION" in tool_desc

    props = _EXTRACTION_TOOL["input_schema"]["properties"]
    for field in ("decisions", "initiatives", "advice"):
        field_desc = props[field]["description"]
        assert "USER" in field_desc, (
            f"{field} description must mention USER attribution: {field_desc!r}"
        )


def test_extraction_gate_has_no_length_floor() -> None:
    """The floor was removed, not moved.

    It began as a combined user+assistant length and discarded short
    instructions answered at length. Relocating it to the user's side only
    relocates the bug: "Do B." (5 chars) and "Done" (4) differ by one
    character and mean opposite things, so no threshold separates them.
    `_is_valid_user_commitment` is the gate that actually tests for a
    commitment.
    """
    from openexecutive.memory import episodic

    for decision in ("Do B.", "Approve option B.", "Kill it.", "no, drop that"):
        assert episodic.should_extract(decision)
    for blank in ("", "  "):
        assert not episodic.should_extract(blank)
    assert not hasattr(episodic, "MIN_USER_CHARS_FOR_EXTRACTION")
    assert not hasattr(episodic, "MIN_TURN_CHARS_FOR_EXTRACTION")


def test_executive_call_sites_use_the_shared_gate() -> None:
    """Every `schedule_extraction` in executive.py must be guarded by a bare
    `should_extract(...)` call and nothing else.

    Checked against the parsed syntax tree rather than the source text. A
    string search pins yesterday's spelling: `if should_extract(user_message)
    and len(user_message) >= 200:` re-introduces the exact defect this work
    removed — a length floor at the call site — while still containing the
    substring a `count(...)` assertion looks for. The AST sees the extra
    condition.

    The call sites live inside long async streaming methods that are not
    unit-testable end-to-end, which is why the guard is pinned structurally
    rather than by driving it.
    """
    import ast
    import inspect

    from openexecutive.orchestrator import executive

    tree = ast.parse(inspect.getsource(executive))

    def _calls_schedule(node: ast.AST) -> bool:
        return any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "schedule_extraction"
            for n in ast.walk(node)
        )

    guards = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and _calls_schedule(node)
    ]
    assert len(guards) == 2, (
        "expected exactly two guarded extraction sites (stream_chat and "
        f"stream_chat_with_committee), found {len(guards)}"
    )

    for guard in guards:
        test = guard.test
        assert isinstance(test, ast.Call), (
            f"line {guard.lineno}: the guard must be a bare should_extract(...) "
            f"call, not {type(test).__name__} — an `and len(...) >= N` clause "
            "is the length floor coming back"
        )
        assert isinstance(test.func, ast.Name) and test.func.id == "should_extract", (
            f"line {guard.lineno}: guard must call should_extract"
        )
        # The speaker's own words (`memory_text` when given), not the prompt:
        # a quoted Executive email or a briefing card's body in the prompt
        # would satisfy the verbatim-quote gate with the Executive's words.
        assert [a.id for a in test.args if isinstance(a, ast.Name)] == ["speaker_text"], (
            f"line {guard.lineno}: guard must read speaker_text, not the prompt"
        )
        # The speaker check is not optional: a channel turn carries someone
        # else's words, and the quote validator cannot tell whose they are.
        assert {kw.arg for kw in test.keywords} == {"origin_channel", "person_id"}, (
            f"line {guard.lineno}: guard must pass the speaker's channel and id"
        )

    # No extraction may be scheduled outside a guard.
    guarded = {id(n) for g in guards for n in ast.walk(g)}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "schedule_extraction"
        ):
            assert id(node) in guarded, f"line {node.lineno}: unguarded extraction"


# ---------------------------------------------------------------------------
# user_commitment_quote validator + schema enforcement
#
# These tests pin the structural fix added after PR #227 shipped: the
# prompt-only approach kept losing — even with the source-attribution rule
# in three places, the extraction LLM still logged the executive's
# recommendations as user commitments (see decision #258, 2026-05-27).
# The fix makes every item carry a `user_commitment_quote` that must:
#   (a) actually appear in the USER QUESTION text, and
#   (b) not end in a question mark.
# A deterministic post-LLM validator drops items that fail. These tests
# guard the schema fields, the validator's rules, and the drop behaviour.
# ---------------------------------------------------------------------------


def test_extraction_schema_requires_user_commitment_quote_on_every_item() -> None:
    """All three item types must require a user_commitment_quote so the LLM
    can't ship an item without quoting the user. If a future refactor drops
    the field from one category, the over-attribution bug returns there.
    """
    from openexecutive.memory.episodic import _EXTRACTION_TOOL

    props = _EXTRACTION_TOOL["input_schema"]["properties"]
    for field in ("decisions", "initiatives", "advice"):
        item_schema = props[field]["items"]
        item_props = item_schema["properties"]
        required = item_schema["required"]
        assert "user_commitment_quote" in item_props, (
            f"{field} item schema is missing user_commitment_quote property"
        )
        assert "user_commitment_quote" in required, (
            f"{field} item schema does not REQUIRE user_commitment_quote"
        )


def test_is_valid_user_commitment_accepts_real_declarative_quote() -> None:
    from openexecutive.memory.episodic import _is_valid_user_commitment

    user = "OK, we're going with fixed POC over hourly for the first $30K deal."
    assert _is_valid_user_commitment("we're going with fixed POC", user) is True
    # Case + whitespace normalization tolerates light formatting drift.
    assert _is_valid_user_commitment("  We're Going With Fixed POC  ", user) is True


def test_is_valid_user_commitment_rejects_questions() -> None:
    """The dominant failure mode: LLM quotes the user's own question and
    pretends it's a commitment. The validator must reject quotes ending in '?'.
    """
    from openexecutive.memory.episodic import _is_valid_user_commitment

    user = "Should we scope the first $30K deal as fixed POC or hourly engagement?"
    # The exact user question — verbatim from msg, but it's a question.
    assert _is_valid_user_commitment(
        "Should we scope the first $30K deal as fixed POC or hourly engagement?",
        user,
    ) is False


def test_is_valid_user_commitment_rejects_quote_not_in_user_message() -> None:
    """Catch the LLM hallucinating a 'user said X' line. If the quote isn't
    in the user message, drop the item even if it reads plausibly.
    """
    from openexecutive.memory.episodic import _is_valid_user_commitment

    user = "Should we go with fixed POC?"
    # Plausible-sounding commitment, but the user never said it.
    assert _is_valid_user_commitment(
        "we're going with fixed POC",
        user,
    ) is False


def test_is_valid_user_commitment_rejects_empty_and_whitespace() -> None:
    from openexecutive.memory.episodic import _is_valid_user_commitment

    user = "we're shipping the rebrand on Friday"
    assert _is_valid_user_commitment("", user) is False
    assert _is_valid_user_commitment("   ", user) is False
    assert _is_valid_user_commitment("\n\t  \n", user) is False


def test_is_valid_user_commitment_handles_smart_quotes() -> None:
    """iOS / macOS auto-substitute straight apostrophes with U+2019. The LLM
    emits ASCII straight quotes. Without folding, every iPhone-typed "We're
    going with X" commitment is silently dropped — the validator must
    bridge that gap or it becomes invisibly Apple-hostile.
    """
    from openexecutive.memory.episodic import _is_valid_user_commitment

    # User typed on iPhone (curly), LLM quoted ASCII straight — must match.
    user_curly = "OK, we’re going with fixed POC."
    quote_ascii = "we're going with fixed POC"
    assert _is_valid_user_commitment(quote_ascii, user_curly) is True

    # And the reverse direction (LLM somehow quoted curly, user ASCII).
    user_ascii = "OK, we're going with fixed POC."
    quote_curly = "we’re going with fixed POC"
    assert _is_valid_user_commitment(quote_curly, user_ascii) is True


def test_is_valid_user_commitment_rejects_substring_inside_user_question() -> None:
    """The original PR #228 design rejected only quotes that themselves ended
    in '?'. Reviewer pointed out the obvious adjacent failure: the LLM lifts
    a fragment of a user's question that doesn't itself end in '?'. The
    validator must scan forward in the user message and reject when the
    next sentence terminator is '?'.
    """
    from openexecutive.memory.episodic import _is_valid_user_commitment

    # User asked a question; LLM tries to lift a fragment as a commitment.
    assert _is_valid_user_commitment(
        "killing project X",
        "Should we be killing project X or pivoting?",
    ) is False

    # Same shape as the May 27 incident's adjacent failure mode.
    assert _is_valid_user_commitment(
        "scope the first $30K deal as fixed POC",
        "Should we scope the first $30K deal as fixed POC or hourly engagement?",
    ) is False


def test_is_valid_user_commitment_accepts_when_terminator_is_period_then_question() -> None:
    """A turn that COMMITS in the first sentence and ASKS in the second is
    fine: 'We're killing X. Should we tell investors?' The quote 'killing X'
    sits inside a sentence terminated by '.', not '?'. Don't over-reject.
    """
    from openexecutive.memory.episodic import _is_valid_user_commitment

    assert _is_valid_user_commitment(
        "killing X",
        "We're killing X. Should we tell investors?",
    ) is True


def test_is_valid_user_commitment_accepts_quote_at_end_of_message_with_no_terminator() -> None:
    """A bare declarative without trailing punctuation should still pass —
    running off the end of the message without finding any terminator is
    a valid accepting condition.
    """
    from openexecutive.memory.episodic import _is_valid_user_commitment

    assert _is_valid_user_commitment(
        "we're going with fixed POC",
        "we're going with fixed POC",
    ) is True


def test_is_valid_user_commitment_rejects_embedded_question_mark() -> None:
    """A quote that itself contains a '?' anywhere isn't a clean commitment,
    even if it doesn't END with one. Cheap defensive check.
    """
    from openexecutive.memory.episodic import _is_valid_user_commitment

    user = "Hmm? OK we're going with X."
    # Quote contains '?' in the middle — reject.
    assert _is_valid_user_commitment("Hmm? OK we're going with X", user) is False


def test_is_valid_user_commitment_accepts_later_occurrence_after_self_correction() -> None:
    """User self-corrects in the same turn: asks the question, then commits.
    The FIRST occurrence of the quote sits inside the question; the SECOND
    occurrence is a real declarative commitment. The validator must walk
    occurrences and accept on the second one.

    Without this, a very common shape ("Should we kill X? Yes, kill X.")
    silently loses the commitment.
    """
    from openexecutive.memory.episodic import _is_valid_user_commitment

    assert _is_valid_user_commitment(
        "kill X",
        "Should we kill X? Yes, kill X.",
    ) is True

    # And the inverse — only the question occurrence — still rejects.
    assert _is_valid_user_commitment(
        "kill X",
        "Should we kill X or not?",
    ) is False


def test_is_valid_user_commitment_treats_newline_as_sentence_terminator() -> None:
    """A bare newline between a commitment and a follow-up question — common
    in chat, where users press Return instead of typing a period — must be
    treated as an accepting sentence boundary. Without this, the scanner
    walks across the newline and rejects on the trailing question mark.
    """
    from openexecutive.memory.episodic import _is_valid_user_commitment

    assert _is_valid_user_commitment(
        "killing X",
        "we are killing X\nShould we tell investors?",
    ) is True

    # Multiple newlines — same outcome.
    assert _is_valid_user_commitment(
        "going with fixed POC",
        "OK, going with fixed POC\n\nShould we do a midpoint demo?",
    ) is True


def test_extract_and_store_drops_decision_with_question_quote(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the LLM returns a decision whose quote is the user's
    question. The validator drops it before SQL, so list_decisions is empty.
    This reproduces the May 27 incident in miniature.
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from openexecutive.memory import episodic as ep

    fake_tool_use = SimpleNamespace(
        type="tool_use",
        name="store_memories",
        input={
            "decisions": [
                {
                    "domain": "strategy",
                    "summary": "First $30K deal will be fixed-scope POC, not hourly.",
                    "rationale": "Fixed scope anchors to outcomes.",
                    "user_commitment_quote": (
                        "Should we scope the first $30K deal as fixed POC or hourly engagement?"
                    ),
                }
            ],
            "initiatives": [],
            "advice": [],
        },
    )
    fake_response = SimpleNamespace(content=[fake_tool_use])
    create_mock = AsyncMock(return_value=fake_response)
    fake_provider = SimpleNamespace(messages_create=create_mock)
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: fake_provider)
    monkeypatch.setattr(
        "openexecutive.config.get_settings",
        lambda: SimpleNamespace(routing_model="claude-test"),
    )

    user_msg = "Should we scope the first $30K deal as fixed POC or hourly engagement?"
    asst_msg = "Fixed POC. Don't sell hours. [long opinionated reply]"
    asyncio.run(ep.extract_and_store(user_msg, asst_msg, db_path=db))

    assert ep.list_decisions(db_path=db) == [], (
        "decision with question-mark quote must be dropped by the validator"
    )
    assert create_mock.await_count == 2, (
        "a bad quote earns one corrective call; the replayed quote is rejected again"
    )


def test_extract_and_store_stores_decision_with_valid_quote(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive case: when the user does declaratively commit and the LLM
    quotes the commitment verbatim, the decision lands in SQL.
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from openexecutive.memory import episodic as ep

    fake_tool_use = SimpleNamespace(
        type="tool_use",
        name="store_memories",
        input={
            "decisions": [
                {
                    "domain": "strategy",
                    "summary": "Going with fixed-scope POC for the first $30K deal.",
                    "rationale": "User committed.",
                    "user_commitment_quote": "we're going with fixed POC",
                }
            ],
            "initiatives": [],
            "advice": [],
        },
    )
    fake_response = SimpleNamespace(content=[fake_tool_use])
    create_mock = AsyncMock(return_value=fake_response)
    fake_provider = SimpleNamespace(messages_create=create_mock)
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: fake_provider)
    monkeypatch.setattr(
        "openexecutive.config.get_settings",
        lambda: SimpleNamespace(routing_model="claude-test"),
    )

    user_msg = "OK, we're going with fixed POC over hourly for the first $30K deal."
    asst_msg = "Smart call. Here's how to structure the SOW..."
    asyncio.run(ep.extract_and_store(user_msg, asst_msg, db_path=db))

    decisions = ep.list_decisions(db_path=db)
    assert len(decisions) == 1
    assert "fixed-scope POC" in decisions[0].summary


def test_extract_and_store_drops_initiative_with_hallucinated_quote(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the LLM invents a quote the user never said, drop the initiative
    even though the title and summary read plausibly.
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from openexecutive.memory import episodic as ep

    fake_tool_use = SimpleNamespace(
        type="tool_use",
        name="store_memories",
        input={
            "decisions": [],
            "initiatives": [
                {
                    "title": "Synchrony POC",
                    "status": "planned",
                    "summary": "Fixed-scope POC for the Synchrony account.",
                    "user_commitment_quote": "we're kicking off the Synchrony POC next week",
                }
            ],
            "advice": [],
        },
    )
    fake_response = SimpleNamespace(content=[fake_tool_use])
    create_mock = AsyncMock(return_value=fake_response)
    fake_provider = SimpleNamespace(messages_create=create_mock)
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: fake_provider)
    monkeypatch.setattr(
        "openexecutive.config.get_settings",
        lambda: SimpleNamespace(routing_model="claude-test"),
    )

    # User never said anything resembling the quote.
    user_msg = "How should I think about the Synchrony opportunity?"
    asst_msg = "Here's what I'd do for Synchrony..."
    asyncio.run(ep.extract_and_store(user_msg, asst_msg, db_path=db))

    assert ep.list_initiatives(db_path=db) == [], (
        "initiative with hallucinated user_commitment_quote must be dropped"
    )
    assert create_mock.await_count == 2, (
        "a bad quote earns one corrective call; the replayed quote is rejected again"
    )
