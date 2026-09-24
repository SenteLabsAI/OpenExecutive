"""Unit tests for openexecutive.briefing.narrative_cache."""
from __future__ import annotations

from pathlib import Path

from openexecutive.briefing import narrative_cache


def test_get_put_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "nc.db"
    narrative_cache.initialize_db(db)
    assert narrative_cache.get(db_path=db) is None

    narrative_cache.put(
        narrative_cache.BriefingNarrative(
            scope=narrative_cache.DEFAULT_SCOPE,
            input_hash="h1",
            narrative_text="hello",
            generated_at=narrative_cache.utc_now_iso(),
        ),
        db_path=db,
    )
    got = narrative_cache.get(db_path=db)
    assert got is not None
    assert got.narrative_text == "hello"
    assert got.input_hash == "h1"


def test_put_upserts_on_scope(tmp_path: Path) -> None:
    db = tmp_path / "nc.db"
    for text, h in (("v1", "h1"), ("v2", "h2")):
        narrative_cache.put(
            narrative_cache.BriefingNarrative(
                scope=narrative_cache.DEFAULT_SCOPE, input_hash=h,
                narrative_text=text, generated_at=narrative_cache.utc_now_iso(),
            ),
            db_path=db,
        )
    got = narrative_cache.get(db_path=db)
    assert got is not None and got.narrative_text == "v2" and got.input_hash == "h2"


def _ctx(today_data: dict, activity: list | None = None) -> str:
    from openexecutive.briefing.narrative import render_briefing_context

    return render_briefing_context(
        period_label="2026-09-20", today_data=today_data, activity=activity or [],
    )


def test_hash_stable_for_same_state() -> None:
    data = {
        "proposals": [{"headline": "A", "category": "action"}],
        "departments": [{"title": "Fin", "slug": "fin", "at_risk_count": 1,
                         "off_track_count": 0, "awaiting_count": 0}],
        "people": [{"id": 3, "full_name": "Dana", "role": "CFO", "awaiting_count": 2}],
    }
    assert narrative_cache.build_narrative_input_hash(_ctx(data)) == \
        narrative_cache.build_narrative_input_hash(_ctx(dict(data)))


def test_hash_changes_when_proposals_change() -> None:
    base = {"proposals": [{"headline": "A", "category": "action"}], "departments": [], "people": []}
    changed = {"proposals": [{"headline": "B", "category": "action"}], "departments": [], "people": []}
    assert narrative_cache.build_narrative_input_hash(_ctx(base)) != \
        narrative_cache.build_narrative_input_hash(_ctx(changed))


def test_hash_differs_by_scope() -> None:
    # Same content, different viewer → distinct cache keys (no cross-user reuse).
    ctx = _ctx({"proposals": [], "departments": [], "people": []})
    assert narrative_cache.build_narrative_input_hash(ctx, "principal") != \
        narrative_cache.build_narrative_input_hash(ctx, "person:5")


def test_hash_ignores_volatile_fields() -> None:
    # created_at / alert_id / score are not rendered into the header context,
    # so they cannot churn the key. This now holds by construction — the key is
    # a hash of the rendered context — rather than by a hand-maintained field
    # list that had to be remembered.
    a = {"proposals": [{"headline": "A", "category": "action", "created_at": "2026-01-01", "score": 40}],
         "departments": [], "people": []}
    b = {"proposals": [{"headline": "A", "category": "action", "created_at": "2026-12-31", "score": 99}],
         "departments": [], "people": []}
    assert narrative_cache.build_narrative_input_hash(_ctx(a)) == \
        narrative_cache.build_narrative_input_hash(_ctx(b))


def test_hash_refuses_a_today_data_dict() -> None:
    """The signature changed from the `today_data` dict to the rendered string.
    A dict is JSON-serialisable, so without this guard a stale caller would
    hash silently and key the entry on input the model never saw."""
    import pytest

    with pytest.raises(TypeError, match="RENDERED context string"):
        narrative_cache.build_narrative_input_hash(
            {"proposals": [], "departments": [], "people": []}  # type: ignore[arg-type]
        )
