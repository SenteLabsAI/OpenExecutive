"""The research watchlist policy: grounded proposals go straight in, uncertain
ones become suggestions, declines are remembered, research watches retire."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.alerts.store import initialize_db as initialize_alerts_db
from openexecutive.audit import AuditLogger, set_audit_logger
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.memory.episodic import initialize_db as initialize_episodic_db
from openexecutive.monitoring import store as ms
from openexecutive.monitoring.models import (
    DECLINE_KIND_EXPIRED,
    DECLINE_KIND_EXPLICIT,
    MODE_ACTIVE,
    MODE_DRY_RUN,
    ORIGIN_RESEARCH,
    ORIGIN_RESEARCH_PROPOSED,
)
from openexecutive.monitoring.research import watch_policy as wp
from openexecutive.monitoring.research.models import ResearchFinding
from openexecutive.monitoring.validation import normalize_target, registrable_domain


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL validation resolves DNS; keep the policy tests offline."""
    from openexecutive.orchestrator import watchlist_tools as wt

    monkeypatch.setattr(wp, "validate_target_url", lambda url: (True, ""))
    monkeypatch.setattr(wt, "validate_target_url", lambda url: (True, ""))

    async def _passthrough(signal_type: str, target: str, config: dict) -> tuple[str, str, dict]:
        return signal_type, target, config

    monkeypatch.setattr(wt, "validate_and_normalize_target", _passthrough)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "policy.db"
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    monkeypatch.setattr("openexecutive.alerts.store.DB_PATH", db_path)
    monkeypatch.setattr("openexecutive.departments.store.DB_PATH", db_path)
    monkeypatch.setattr("openexecutive.people.store.DB_PATH", db_path)
    initialize_episodic_db(db_path)
    initialize_alerts_db(db_path)
    ms.initialize_db(db_path)
    from openexecutive.departments import registry as dept_registry
    from openexecutive.departments import store as dept_store
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    dept_store.initialize_db(db_path)
    people_store.initialize_db(db_path)
    dept_registry.invalidate()
    people_registry.invalidate()
    audit = AuditLogger(db_path=db_path)
    audit.initialize_db()
    set_audit_logger(audit)
    yield db_path
    dept_registry.invalidate()
    people_registry.invalidate()


def _profile() -> CompanyProfile:
    return CompanyProfile.model_validate({
        "name": "Sente Labs",
        "competitive_landscape": {"primary_competitors": ["Acme Corp", "Globex"]},
        "strategic_priorities": {"current_year": ["Expand into EU enterprise accounts"]},
        "vendors": ["Stripe"],
        "tickers": ["ACME"],
    })


def _finding(**kw: Any) -> ResearchFinding:
    base: dict[str, Any] = {
        "title": "Acme cut prices",
        "summary": "Acme Corp cut list prices 20% on 2026-09-01.",
        "confidence": "high",
        "relevant_urls": ["https://www.acme.com/blog/pricing"],
        "source_specialist": "cso",
    }
    base.update(kw)
    return ResearchFinding(**base)


def _proposal(**kw: Any) -> wp.WatchProposal:
    base: dict[str, Any] = {
        "slug": "rss-acme-blog", "signal_type": "rss",
        "target": "https://www.acme.com/blog/feed.xml",
        "grounding_entity": "Acme Corp", "rationale": "Competitor pricing moves",
        "finding_index": 0, "certainty": "confident",
    }
    base.update(kw)
    base["normalized_target"] = normalize_target(base["signal_type"], base["target"])
    return wp.WatchProposal(**base)


def _ctx(existing: list[Any] | None = None, **kw: Any) -> wp.PolicyContext:
    profile = _profile()
    initiatives = [SimpleNamespace(title="Launch Helios API", status="active")]
    existing = existing or []
    return wp.PolicyContext(
        vocabulary=wp.grounding_vocabulary(profile, initiatives, existing),
        priority_terms=wp.priority_terms(profile),
        existing=existing,
        **kw,
    )


# --------------------------------------------------------------------- #
# Vocabulary + matching
# --------------------------------------------------------------------- #


def test_vocabulary_covers_profile_initiatives_and_watch_labels(db: Path) -> None:
    ms.insert_watchlist_item(
        slug="stock-hubs", signal_type="stock", target="HUBS",
        config={"display_name": "HubSpot"}, db_path=db,
    )
    vocab = wp.grounding_vocabulary(
        _profile(), [SimpleNamespace(title="Launch Helios API")], ms.list_watchlist(db_path=db),
    )
    assert vocab["sente labs"].kind == wp.KIND_COMPANY
    assert vocab["acme corp"].kind == wp.KIND_COMPETITOR
    assert vocab["stripe"].kind == wp.KIND_VENDOR
    assert vocab["acme"].kind == wp.KIND_TICKER
    assert vocab["launch helios api"].kind == wp.KIND_INITIATIVE
    assert vocab["hubspot"].kind == wp.KIND_WATCH and vocab["hubs"].kind == wp.KIND_WATCH
    assert all(entry.department == "" for entry in vocab.values())


def test_match_entity_is_token_based_and_prefers_company_data() -> None:
    vocab = {"acme corp": wp.KIND_COMPETITOR, "acme": wp.KIND_WATCH, "stripe": wp.KIND_VENDOR}
    assert wp.match_entity("Acme", vocab) == ("acme corp", wp.KIND_COMPETITOR)
    assert wp.match_entity("ACME CORP.", vocab) == ("acme corp", wp.KIND_COMPETITOR)
    assert wp.match_entity("Stripe Inc", vocab) == ("stripe", wp.KIND_VENDOR)
    assert wp.match_entity("Initech", vocab) is None
    assert wp.match_entity("", vocab) is None


def test_priority_terms_drop_stopwords() -> None:
    # "expand", "into", "enterprise" are stopwords; only distinctive words remain.
    assert wp.priority_terms(_profile()) == ["accounts"]


def test_normalize_target_and_domain() -> None:
    assert normalize_target("stock", " acme ") == "ACME"
    # Respellings of one path collapse: dot segments, percent-encoding, a
    # trailing host dot.
    assert normalize_target("rss", "https://competitor.example./x/../blog/./%66eed") == "https://competitor.example/blog/feed"
    assert normalize_target("query", "site:https://acme.com  Acme   Pricing") == "site:https://acme.com acme pricing"
    # scheme, www., query, fragment, path case and trailing slash never
    # distinguish two spellings of one source.
    assert normalize_target("rss", "HTTP://www.Acme.com/Feed/?x=1#top") == "https://acme.com/feed"
    assert normalize_target("rss", "https://acme.com/") == "https://acme.com"
    assert normalize_target("query", "  Acme   Corp pricing ") == "acme corp pricing"
    assert registrable_domain("https://www.acme.com/blog") == "acme.com"
    assert registrable_domain("ACME") == ""


# --------------------------------------------------------------------- #
# Classification ledger
# --------------------------------------------------------------------- #


def test_grounded_own_source_high_confidence_is_direct() -> None:
    d = wp.classify(_proposal(), _finding(), _ctx())
    # +2 competitor, +1 own domain, +1 high confidence = 4
    assert d.tier == wp.TIER_DIRECT and d.score == 4
    assert d.grounding_kind == wp.KIND_COMPETITOR


def test_ticker_is_own_source_only_when_it_is_the_entity() -> None:
    p = _proposal(slug="stock-acme", signal_type="stock", target="ACME", grounding_entity="ACME")
    d = wp.classify(p, _finding(title="ACME slides", summary="ACME fell 8% on 2026-09-01."), _ctx())
    assert d.tier == wp.TIER_DIRECT and d.grounding_kind == wp.KIND_TICKER
    # A profile ticker that belongs to someone else lends nothing to "Acme Corp".
    other = _proposal(slug="stock-msft", signal_type="stock", target="MSFT", grounding_entity="Acme Corp")
    profile = CompanyProfile.model_validate({
        "name": "Sente Labs", "competitive_landscape": {"primary_competitors": ["Acme Corp"]},
        "tickers": ["MSFT"],
    })
    ctx = wp.PolicyContext(vocabulary=wp.grounding_vocabulary(profile, [], []), priority_terms=[], existing=[])
    d2 = wp.classify(other, _finding(), ctx)
    assert d2.tier == wp.TIER_SUGGEST and "own source" not in " ".join(d2.reasons)


def _stripe_finding(**kw: Any) -> ResearchFinding:
    return _finding(title="Stripe incident", summary="Stripe reported degraded payments on 2026-09-02.",
                    relevant_urls=["https://status.stripe.com/incidents/abc"], source_specialist="coo", **kw)


def test_vendor_status_page_is_direct() -> None:
    p = _proposal(
        slug="vendor-stripe", signal_type="vendor_status",
        target="https://status.stripe.com/feed.atom", grounding_entity="Stripe",
    )
    d = wp.classify(p, _stripe_finding(), _ctx())
    assert d.tier == wp.TIER_DIRECT and "own source" in " ".join(d.reasons)


def _initech_finding(**kw: Any) -> ResearchFinding:
    base: dict[str, Any] = dict(title="Initech raised", summary="Initech raised a Series B on 2026-09-03.",
                                relevant_urls=["https://initech.com/blog/series-b"])
    base.update(kw)
    return _finding(**base)


def test_entity_not_in_company_data_is_never_direct() -> None:
    p = _proposal(grounding_entity="Initech", target="https://initech.com/feed.xml")
    d = wp.classify(p, _initech_finding(source_specialist="cso,cfo"), _ctx())
    assert d.tier == wp.TIER_SUGGEST
    assert d.score == 2  # own-source +1 needs an entity; high +1, consensus +1


def test_medium_confidence_grounded_needs_more_corroboration() -> None:
    d = wp.classify(_proposal(), _finding(confidence="medium"), _ctx())
    assert d.tier == wp.TIER_SUGGEST and d.score == 3
    d2 = wp.classify(_proposal(), _finding(confidence="medium", source_specialist="cso,cfo"), _ctx())
    assert d2.tier == wp.TIER_DIRECT and d2.score == 4


def test_unsure_only_downgrades() -> None:
    d = wp.classify(_proposal(certainty="unsure"), _finding(), _ctx())
    assert d.tier == wp.TIER_SUGGEST and "unsure" in d.reasons[-1]
    # 'confident' cannot lift an ungrounded proposal.
    d2 = wp.classify(_proposal(grounding_entity="Initech", certainty="confident"), _finding(), _ctx())
    assert d2.tier == wp.TIER_SUGGEST


def test_query_is_always_a_suggestion() -> None:
    p = _proposal(slug="q-acme", signal_type="query", target="Acme Corp pricing", grounding_entity="Acme Corp")
    d = wp.classify(p, _finding(source_specialist="cso,cfo"), _ctx())
    assert d.tier == wp.TIER_SUGGEST and "standing web queries" in d.reasons[-1]


def test_same_site_already_watched_is_rejected(db: Path) -> None:
    ms.insert_watchlist_item(
        slug="rss-acme-news", signal_type="rss", target="https://acme.com/news/feed", db_path=db,
    )
    existing = ms.list_watchlist(db_path=db)
    d = wp.classify(_proposal(), _finding(), _ctx(existing))
    assert d.tier == wp.TIER_REJECT and "already watched" in d.reasons[-1]


def test_ceiling_rejects_adds_and_suggestions(db: Path) -> None:
    for i in range(3):
        ms.insert_watchlist_item(slug=f"stock-x{i}", signal_type="stock", target=f"X{i}", db_path=db)
    ctx = _ctx(ms.list_watchlist(db_path=db), settings=wp.PolicySettings(max_enabled=3))
    d = wp.classify(_proposal(), _finding(), ctx)
    assert d.tier == wp.TIER_REJECT and "ceiling" in d.reasons[-1]
    unsure = _proposal(grounding_entity="Initech", target="https://initech.com/feed.xml", certainty="unsure")
    assert wp.classify(unsure, _finding(), ctx).tier == wp.TIER_REJECT


def test_nothing_vouching_is_rejected_not_suggested() -> None:
    bare = _proposal(grounding_entity="", target="https://randomblog.example/feed", certainty="unsure",
                     finding_index=None)
    assert wp.classify(bare, None, _ctx()).tier == wp.TIER_REJECT
    weak = _proposal(grounding_entity="Initech", target="https://initech.com/feed.xml", certainty="unsure")
    assert wp.classify(weak, _initech_finding(confidence="medium"), _ctx()).tier == wp.TIER_REJECT
    assert wp.classify(weak, _initech_finding(confidence="high"), _ctx()).tier == wp.TIER_SUGGEST


def test_own_source_uses_the_site_label_only() -> None:
    assert wp._site_label("status.acme.com") == "acme"
    assert wp._site_label("acme.co.uk") == "acme"
    assert wp._site_label("api.unrelated-vendor.com") == "unrelated-vendor"
    # The label must be the entity's leading name or its words run together,
    # never a generic trailing word: "Acme Payments" is not payments.io.
    vocab = {"acme payments": wp.KIND_VENDOR}
    generic = _proposal(slug="rss-pay", target="https://payments.io/feed", grounding_entity="Acme Payments")
    assert not wp._is_own_source(generic, "acme payments", vocab)
    for host in ("https://acme.com/feed", "https://status.acmepayments.com/feed"):
        own = _proposal(slug="rss-own", target=host, grounding_entity="Acme Payments")
        assert wp._is_own_source(own, "acme payments", vocab)
    p = _proposal(slug="pw", signal_type="page_watch", target="https://api.unrelated.com/pricing",
                  grounding_entity="Launch Helios API")
    assert "own source" not in " ".join(wp.classify(p, _finding(), _ctx()).reasons)
    p2 = _proposal(slug="stock-msft", signal_type="stock", target="MSFT", grounding_entity="Acme Corp")
    assert "own source" not in " ".join(wp.classify(p2, _finding(), _ctx()).reasons)


def test_vocabulary_prefers_the_strongest_kind() -> None:
    profile = CompanyProfile.model_validate({"name": "Acme", "vendors": ["Acme"], "tickers": ["ACME"]})
    vocab = wp.grounding_vocabulary(profile, [SimpleNamespace(title="Acme")], [])
    assert vocab["acme"].kind == wp.KIND_COMPANY


def test_history_adjusts_only_with_enough_samples() -> None:
    counts = {("rss", wp.KIND_COMPETITOR): {"approved": 1, "declined": 4}}
    d = wp.classify(_proposal(), _finding(), _ctx(outcome_counts=counts))
    assert d.score == 3 and d.tier == wp.TIER_SUGGEST  # −1 from history
    few = {("rss", wp.KIND_COMPETITOR): {"approved": 0, "declined": 3}}
    assert wp.classify(_proposal(), _finding(), _ctx(outcome_counts=few)).score == 4


# --------------------------------------------------------------------- #
# Quiet defaults
# --------------------------------------------------------------------- #


def test_quiet_defaults_add_keywords_and_no_floor() -> None:
    cadence, floor, trigger = wp.quiet_defaults(_proposal(), ["accounts"])
    assert cadence == "daily" and floor.value == "low"
    kws = trigger["keywords"]
    assert "pricing" in kws and "accounts" in kws
    # The entity's own name is never a keyword: feed summaries carry the
    # feed label, so it would match every entry.
    assert "acme" not in kws and "corp" not in kws
    _, _, stock_trigger = wp.quiet_defaults(_proposal(signal_type="stock", target="ACME"), [])
    assert stock_trigger == {"abs_change_pct_gte": 5}
    cadence_pw, _, _ = wp.quiet_defaults(
        _proposal(signal_type="page_watch", target="https://acme.com/pricing"), [],
    )
    assert cadence_pw == "daily"


# --------------------------------------------------------------------- #
# apply_proposals
# --------------------------------------------------------------------- #


def test_apply_adds_direct_and_files_suggestion(db: Path) -> None:
    findings = [_finding(), _initech_finding(confidence="high")]
    proposals = [
        _proposal(),
        _proposal(slug="rss-initech", target="https://initech.com/feed.xml",
                  grounding_entity="Initech", finding_index=1, certainty="unsure"),
    ]
    out = wp.apply_proposals(proposals, findings, _ctx(), db_path=db)
    assert [o["outcome"] for o in out] == ["added", "suggested"]
    rows = {w.slug: w for w in ms.list_watchlist(db_path=db)}
    acme, initech = rows["rss-acme-blog"], rows["rss-initech"]
    assert acme.origin == ORIGIN_RESEARCH and acme.mode == MODE_ACTIVE
    assert acme.notes == "Competitor pricing moves" and acme.severity_floor.value == "low"
    assert acme.config_json["_policy"]["grounding_kind"] == wp.KIND_COMPETITOR
    assert initech.origin == ORIGIN_RESEARCH_PROPOSED and initech.mode == MODE_DRY_RUN
    assert ms.list_pending_suggestions(db_path=db)[0].slug == "rss-initech"


def test_a_watch_grounded_on_a_goal_is_for_that_goal(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.departments import store as departments
    from openexecutive.people import store as people

    monkeypatch.setattr(departments, "DB_PATH", db)
    people.initialize_db(db)
    departments.initialize_db(db)
    goal_id = departments.insert_goal(
        None, period_value="2031-Q1", key_result="Close the books in 5 days", target="5 days", db_path=db,
    )
    on_goal = SimpleNamespace(grounding_kind=wp.KIND_DEPARTMENT_SCOPE, entity=wp._norm("Close the books in 5 days"))
    assert wp._goal_for(on_goal, db) == goal_id  # type: ignore[arg-type]
    other = SimpleNamespace(grounding_kind=wp.KIND_COMPETITOR, entity=wp._norm("Close the books in 5 days"))
    assert wp._goal_for(other, db) is None  # type: ignore[arg-type]
    unknown = SimpleNamespace(grounding_kind=wp.KIND_DEPARTMENT_SCOPE, entity="something else")
    assert wp._goal_for(unknown, db) is None  # type: ignore[arg-type]


def test_apply_honours_budgets_and_duplicate_targets(db: Path) -> None:
    findings = [_finding(title="ACME cuts prices"), _stripe_finding(), _initech_finding(confidence="high")]
    proposals = [
        _proposal(slug="stock-acme", signal_type="stock", target="ACME", grounding_entity="ACME"),
        _proposal(slug="stock-acme-2", signal_type="stock", target="acme", grounding_entity="ACME"),  # same target
        _proposal(slug="vendor-stripe", signal_type="vendor_status",
                  target="https://status.stripe.com/feed.atom", grounding_entity="Stripe", finding_index=1),
        _proposal(slug="rss-acme-blog"),  # third direct → budget → suggestion
        _proposal(slug="rss-initech", target="https://initech.com/feed.xml", grounding_entity="Initech",
                  finding_index=2),  # not in company data: the weakest → loses the budget
        _proposal(slug="rss-globex", target="https://globex.com/feed.xml", grounding_entity="Globex",
                  certainty="unsure"),  # a competitor, own source: outranks Initech despite filing last
    ]
    ctx = _ctx(settings=wp.PolicySettings(max_direct_adds=2, max_suggestions=2))
    out = wp.apply_proposals(proposals, findings, ctx, db_path=db)
    assert [o["outcome"] for o in out] == [
        "added", "rejected", "added", "suggested", "rejected", "suggested",
    ]
    assert "duplicate" in out[1]["result_preview"]
    assert "budget" in out[4]["result_preview"]
    # The caller's context is not mutated by the run.
    assert ctx.existing == []


# --------------------------------------------------------------------- #
# Declines memory
# --------------------------------------------------------------------- #


def test_explicit_decline_is_permanent_and_expiry_retries_after_90_days(db: Path) -> None:
    ms.insert_decline(normalized_target="https://initech.com/feed.xml",
                      kind=DECLINE_KIND_EXPLICIT, reason="not_relevant", db_path=db)
    ms.insert_decline(normalized_target="https://globex.com/feed.xml",
                      kind=DECLINE_KIND_EXPIRED, reason="expired", db_path=db)
    assert ms.is_declined("https://initech.com/feed.xml", db_path=db)
    assert ms.is_declined("https://globex.com/feed.xml", db_path=db)
    later = datetime.now(UTC) + timedelta(days=91)
    assert ms.is_declined("https://initech.com/feed.xml", now=later, db_path=db)
    assert not ms.is_declined("https://globex.com/feed.xml", now=later, db_path=db)
    # An expiry never downgrades an explicit decline; an explicit decline
    # always overwrites an expiry.
    ms.insert_decline(normalized_target="https://initech.com/feed.xml",
                      kind=DECLINE_KIND_EXPIRED, reason="expired", db_path=db)
    ms.insert_decline(normalized_target="https://globex.com/feed.xml",
                      kind=DECLINE_KIND_EXPLICIT, reason="wrong_source", db_path=db)
    kinds = {d.normalized_target: (d.kind, d.reason) for d in ms.list_declines(db_path=db)}
    assert kinds["https://initech.com/feed.xml"] == (DECLINE_KIND_EXPLICIT, "not_relevant")
    assert kinds["https://globex.com/feed.xml"] == (DECLINE_KIND_EXPLICIT, "wrong_source")
    assert ms.is_declined("https://globex.com/feed.xml", now=later, db_path=db)


@pytest.mark.asyncio
async def test_propose_handler_refuses_declined_target_under_new_slug(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.orchestrator import watchlist_tools as wt

    ms.insert_decline(normalized_target="https://initech.com/feed.xml",
                      kind=DECLINE_KIND_EXPLICIT, reason="wrong_source", db_path=db)
    collector: list[Any] = []
    out = await wt.handle_propose_watch({
        "slug": "rss-initech-again", "signal_type": "rss",
        "target": "HTTP://www.initech.com/Feed.xml/?utm=1", "grounding_entity": "Initech",
        "rationale": "x", "certainty": "confident",
    }, collector)
    assert "declined" in out and collector == []
    # wrong_source blacklists only that target; another Initech source may be proposed.
    other = await wt.handle_propose_watch({
        "slug": "rss-initech-status", "signal_type": "vendor_status",
        "target": "https://status.initech.com/history.atom", "grounding_entity": "Initech",
        "rationale": "x", "certainty": "unsure",
    }, collector)
    assert '"queued"' in other and len(collector) == 1
    # not_relevant blacklists the entity: any Initech source is refused.
    ms.insert_decline(normalized_target="https://initech.com/other", entity="Initech Inc",
                      kind=DECLINE_KIND_EXPLICIT, reason="not_relevant", db_path=db)
    blocked = await wt.handle_propose_watch({
        "slug": "stock-initech", "signal_type": "stock", "target": "INTC",
        "grounding_entity": "initech", "rationale": "x", "certainty": "confident",
    }, collector)
    assert "declined watching" in blocked and len(collector) == 1
    ok = await wt.handle_propose_watch({
        "slug": "stock-acme", "signal_type": "stock", "target": "ACME",
        "grounding_entity": "Acme Corp", "rationale": "y", "certainty": "confident",
        "finding_index": 1,
    }, collector)
    assert '"queued": "stock-acme"' in ok
    assert len(collector) == 2 and collector[-1].finding_index == 0
    dup = await wt.handle_propose_watch({
        "slug": "stock-acme", "signal_type": "stock", "target": "ACME2",
        "grounding_entity": "Acme Corp", "rationale": "y", "certainty": "confident", "finding_index": 0,
    }, collector)
    assert "already proposed" in dup and len(collector) == 2


# --------------------------------------------------------------------- #
# Sweep
# --------------------------------------------------------------------- #


def _backdate(db: Path, slug: str, days: int) -> None:
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE watchlist SET created_at = ? WHERE slug = ?",
            ((datetime.now(UTC) - timedelta(days=days)).isoformat(), slug),
        )


def test_sweep_expires_old_suggestions_as_unreviewed(db: Path) -> None:
    ms.insert_watchlist_item(
        slug="rss-old", signal_type="rss", target="https://old.com/feed", mode=MODE_DRY_RUN,
        origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
    )
    ms.insert_watchlist_item(
        slug="rss-new", signal_type="rss", target="https://new.com/feed", mode=MODE_DRY_RUN,
        origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
    )
    _backdate(db, "rss-old", 15)
    counts = wp.sweep(datetime.now(UTC), db_path=db)
    assert counts["expired"] == 1
    assert [w.slug for w in ms.list_pending_suggestions(db_path=db)] == ["rss-new"]
    decl = ms.list_declines(db_path=db)
    assert decl[0].normalized_target == "https://old.com/feed" and decl[0].kind == DECLINE_KIND_EXPIRED


def test_sweep_expires_a_suggestion_that_already_recorded_signals(db: Path) -> None:
    """Dry-run suggestions poll and record signals; the FK on external_signals
    must not make them un-expirable."""
    from openexecutive.monitoring.models import Signal

    wid = ms.insert_watchlist_item(
        slug="rss-polled", signal_type="rss", target="https://polled.com/feed", mode=MODE_DRY_RUN,
        origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
    )
    ms.insert_signal(Signal(
        watchlist_id=wid, source_kind="rss", source_external_id="e1",
        captured_at=datetime.now(UTC).isoformat(), normalized_summary="s",
        provenance_url="https://polled.com/1", dedup_key="k1",
    ), db_path=db)
    _backdate(db, "rss-polled", 15)
    assert wp.sweep(datetime.now(UTC), db_path=db)["expired"] == 1
    assert ms.get_watchlist_item(wid, db_path=db) is None
    assert ms.list_signals_for_watchlist(wid, db_path=db) == []


def test_sweep_auto_disables_noisy_research_watches_only(db: Path) -> None:
    import sqlite3

    ms.insert_watchlist_item(slug="rss-noisy", signal_type="rss", target="https://noisy.com/feed",
                             origin=ORIGIN_RESEARCH, db_path=db)
    ms.insert_watchlist_item(slug="pw-quiet", signal_type="page_watch", target="https://quiet.com/pricing",
                             origin=ORIGIN_RESEARCH, db_path=db)  # silent = unchanged page, still working
    ms.insert_watchlist_item(slug="rss-mine", signal_type="rss", target="https://mine.com/feed",
                             db_path=db)  # manual: never auto-disabled
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE watchlist SET fired_count = 6, dismiss_count = 3, trust_score = 0.4 "
            "WHERE slug IN ('rss-noisy', 'rss-mine')"
        )
    _backdate(db, "pw-quiet", 45)
    counts = wp.sweep(datetime.now(UTC), db_path=db)
    assert counts["disabled"] == 1
    enabled = {w.slug: w.enabled for w in ms.list_watchlist(db_path=db)}
    assert enabled == {"rss-noisy": False, "pw-quiet": True, "rss-mine": True}


def test_sweep_auto_disables_after_consecutive_poll_failures(db: Path) -> None:
    from openexecutive.audit import log_event

    ms.insert_watchlist_item(slug="rss-broken", signal_type="rss", target="https://broken.com/feed",
                             origin=ORIGIN_RESEARCH, db_path=db)
    for _ in range(2):
        log_event("external_monitor_poll", "Polled rss-broken (rss) — FAILED", actor="external_monitor",
                  details={"watchlist_slug": "rss-broken", "failed": True})
    assert wp.sweep(datetime.now(UTC), db_path=db)["disabled"] == 0
    log_event("external_monitor_poll", "Polled rss-broken (rss) — FAILED", actor="external_monitor",
              details={"watchlist_slug": "rss-broken", "failed": True})
    assert wp.sweep(datetime.now(UTC), db_path=db)["disabled"] == 1


def test_sweep_nudges_once_suggestions_pile_up(db: Path) -> None:
    from openexecutive.alerts.store import list_alerts

    for i in range(3):
        ms.insert_watchlist_item(
            slug=f"rss-s{i}", signal_type="rss", target=f"https://s{i}.com/feed",
            mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
        )
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 0  # too fresh
    _backdate(db, "rss-s0", 8)
    _backdate(db, "rss-s1", 8)
    ms.insert_watchlist_item(
        slug="rss-s3", signal_type="rss", target="https://s3.com/feed",
        mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
    )
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 1
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 0  # coalesced into the open card
    # Acting on the oldest suggestion must not spawn another nudge.
    first = ms.get_watchlist_item_by_slug("rss-s0", db_path=db)
    assert first is not None and first.id is not None and ms.approve_suggestion(first.id, db_path=db)
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 0
    alerts = [a for a in list_alerts(limit=10, db_path=db) if a.source == wp.NUDGE_ALERT_SOURCE]
    assert len(alerts) == 1 and alerts[0].headline == "Watch suggestions waiting for your OK"
    # In words, never slugs or signal types.
    assert "rss-s" not in alerts[0].body and "(rss)" not in alerts[0].body
    assert "• New posts at s1.com" in alerts[0].body


def test_direct_add_needs_a_finding_that_cites_the_source() -> None:
    # Department entity + own source + a decision mention reach the score
    # bar without any finding evidence about the target; that is still only
    # a suggestion — nothing goes live on hearsay.
    decided = SimpleNamespace(summary="Move expense cards to Brex", department="finance", timestamp="")
    unrelated = _finding(confidence="high")  # about Acme, cites acme.com
    d = wp.classify(_brex_proposal(), unrelated, _dept_ctx(recent_decisions=[decided]))
    assert d.score >= wp.DIRECT_THRESHOLD and d.tier == wp.TIER_SUGGEST
    assert "no finding cites this source" in d.reasons
    assert wp.classify(_brex_proposal(), _brex_finding(), _dept_ctx(recent_decisions=[decided])).tier == wp.TIER_DIRECT


def test_finding_points_need_a_finding_about_this_source() -> None:
    # A strong finding about Acme lends nothing to an unrelated attacker URL.
    p = _proposal(target="https://totally-unrelated.attacker.net/feed", grounding_entity="Acme Corp")
    d = wp.classify(p, _finding(source_specialist="cso,cfo"), _ctx())
    assert d.score == 2 and d.tier == wp.TIER_SUGGEST
    assert "does not mention this source" in " ".join(d.reasons)
    # A finding that names the ticker does support a ticker watch on it.
    p2 = _proposal(slug="stock-acme", signal_type="stock", target="ACME", grounding_entity="ACME")
    assert wp.classify(p2, _finding(title="ACME slides 8%"), _ctx()).tier == wp.TIER_DIRECT


def test_direct_add_requires_the_entitys_own_source() -> None:
    # A well-corroborated third-party page about a competitor is the
    # principal's call, never a direct add.
    p = _proposal(target="https://news.example.com/acme-feed.xml", grounding_entity="Acme Corp")
    f = _finding(source_specialist="cso,cfo",
                 relevant_urls=["https://news.example.com/acme-pricing"])
    d = wp.classify(p, f, _ctx())
    assert d.score >= wp.DIRECT_THRESHOLD and d.tier == wp.TIER_SUGGEST
    assert "not the entity's own source" in d.reasons


def test_initiative_or_priority_grounding_is_never_direct() -> None:
    p = _proposal(slug="rss-helios", target="https://helios.com/feed.xml", grounding_entity="Helios API")
    f = _finding(title="Helios API launch", summary="Launch Helios API shipped.",
                 relevant_urls=["https://helios.com/launch"])
    d = wp.classify(p, f, _ctx())
    assert d.grounding_kind == wp.KIND_INITIATIVE and d.score >= wp.DIRECT_THRESHOLD
    assert d.tier == wp.TIER_SUGGEST and "needs a named competitor" in d.reasons[-1]


def test_generic_tokens_do_not_ground() -> None:
    # "enterprise" appears in a priority but is a stopword; "Corp" is too.
    assert wp.match_entity("Enterprise Corp", _ctx().vocabulary) is None
    assert wp.match_entity("Growth", {"drive growth in emea": wp.KIND_PRIORITY}) is None
    # Short names that are whole vocabulary terms still match in longer forms.
    assert wp.match_entity("IBM Corp", {"ibm": wp.KIND_COMPETITOR}) == ("ibm", wp.KIND_COMPETITOR)
    assert wp.match_entity("AWS Lambda", {"aws": wp.KIND_VENDOR}) == ("aws", wp.KIND_VENDOR)


def test_vocabulary_from_url_targets_uses_the_site_label(db: Path) -> None:
    ms.insert_watchlist_item(slug="vendor-stripe", signal_type="vendor_status",
                             target="https://status.stripe.com/history.atom", db_path=db)
    vocab = wp.grounding_vocabulary(CompanyProfile(name="X"), [], ms.list_watchlist(db_path=db))
    assert vocab["stripe"].kind == wp.KIND_WATCH
    assert not any(t.startswith("https") for t in vocab)
    assert wp.match_entity("Status Labs", vocab) is None


def test_quiet_defaults_never_put_keywords_on_a_page_watch() -> None:
    _, _, trigger = wp.quiet_defaults(
        _proposal(signal_type="page_watch", target="https://acme.com/pricing"), ["accounts"],
    )
    assert "keywords" not in trigger


def test_not_relevant_decline_remembers_the_models_entity_when_ungrounded(db: Path) -> None:
    findings = [_initech_finding(confidence="high")]
    out = wp.apply_proposals(
        [_proposal(slug="rss-initech", target="https://initech.com/feed.xml",
                   grounding_entity="Initech Industries", certainty="unsure")],
        findings, _ctx(), db_path=db,
    )
    assert out[0]["outcome"] == "suggested"
    row = ms.get_watchlist_item_by_slug("rss-initech", db_path=db)
    assert row is not None and row.config_json["_policy"]["entity"] == "initech industries"


def test_own_source_ignores_generic_host_tokens() -> None:
    p = _proposal(target="https://labs.example.com/feed.xml", grounding_entity="Sente Labs")
    d = wp.classify(p, _finding(), _ctx())
    assert "own source" not in " ".join(d.reasons)


def test_retired_source_is_not_re_added(db: Path) -> None:
    ms.insert_watchlist_item(slug="rss-acme-old", signal_type="rss", target="https://acme.com/blog/feed.xml",
                             origin=ORIGIN_RESEARCH, enabled=False, db_path=db)
    d = wp.classify(_proposal(), _finding(), _ctx(ms.list_watchlist(db_path=db)))
    assert d.tier == wp.TIER_REJECT


def test_auto_disable_records_a_retryable_decline(db: Path) -> None:
    import sqlite3

    ms.insert_watchlist_item(slug="rss-noisy", signal_type="rss", target="https://noisy.com/feed",
                             origin=ORIGIN_RESEARCH, db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE watchlist SET fired_count = 6, dismiss_count = 3, trust_score = 0.4")
    assert wp.sweep(datetime.now(UTC), db_path=db)["disabled"] == 1
    assert ms.is_declined("https://noisy.com/feed", db_path=db)
    assert not ms.is_declined("https://noisy.com/feed", now=datetime.now(UTC) + timedelta(days=91), db_path=db)


def test_source_url_stamp_only_keeps_public_http_urls(db: Path) -> None:
    f = _finding(relevant_urls=["javascript:alert(1)", "ftp://x/y", "https://www.acme.com/blog/pricing"])
    assert wp._safe_source_url(f) == "https://www.acme.com/blog/pricing"
    out = wp.apply_proposals([_proposal()], [f], _ctx(), db_path=db)
    assert out[0]["outcome"] == "added"
    row = ms.get_watchlist_item_by_slug("rss-acme-blog", db_path=db)
    assert row is not None and row.config_json["_policy"]["source_url"] == "https://www.acme.com/blog/pricing"


def test_insert_failure_is_reported_without_exception_text(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kw: Any) -> int:
        raise RuntimeError("sqlite3.OperationalError: /secret/path locked")

    monkeypatch.setattr(ms, "insert_watchlist_item", boom)
    out = wp.apply_proposals([_proposal()], [_finding()], _ctx(), db_path=db)
    assert out[0]["outcome"] == "rejected" and "see server log" in out[0]["result_preview"]
    assert "/secret/path" not in out[0]["result_preview"]


# --------------------------------------------------------------------- #
# Departments + episodic decisions
# --------------------------------------------------------------------- #


def _department(slug: str, title: str, *, watched: list[str] | None = None,
                scope: list[str] | None = None, head: int | None = None,
                goals: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            slug=slug, title=title, head_person_id=head,
            watched_entities=list(watched or []),
            charter=SimpleNamespace(scope=list(scope or [])),
        ),
        goals=[SimpleNamespace(key_result=g) for g in (goals or [])],
    )


def _finance(head: int | None = 7) -> SimpleNamespace:
    return _department(
        "finance", "Finance", watched=["Brex"], scope=["Expense card programs"],
        goals=["Close the books in 5 days"], head=head,
    )


def _dept_ctx(*departments: Any, existing: list[Any] | None = None, **kw: Any) -> wp.PolicyContext:
    profile = _profile()
    departments = departments or (_finance(),)
    existing = existing or []
    return wp.PolicyContext(
        vocabulary=wp.grounding_vocabulary(profile, [], existing, list(departments)),
        priority_terms=wp.priority_terms(profile),
        existing=existing,
        departments=wp.department_refs(list(departments)),
        **kw,
    )


def _brex_finding(**kw: Any) -> ResearchFinding:
    return _finding(
        title="Brex outage", summary="Brex card authorizations failed for 2h on 2026-09-01.",
        relevant_urls=["https://status.brex.com/incidents/1"], source_specialist="cfo", **kw,
    )


def _brex_proposal(**kw: Any) -> wp.WatchProposal:
    return _proposal(
        slug="vendor-brex", signal_type="vendor_status", target="https://status.brex.com",
        grounding_entity="Brex", rationale="Finance is on Brex cards", **kw,
    )


def test_department_vocabulary_kinds_and_department_merge() -> None:
    finance = _department("finance", "Finance", watched=["Brex", "Acme Corp"],
                          scope=["Expense card programs"], goals=["Close the books in 5 days"])
    vocab = wp.grounding_vocabulary(_profile(), [], [], [finance])
    assert vocab["brex"] == wp.VocabEntry(wp.KIND_DEPARTMENT_ENTITY, "finance")
    assert vocab["expense card programs"] == wp.VocabEntry(wp.KIND_DEPARTMENT_SCOPE, "finance")
    assert vocab["close the books in 5 days"].kind == wp.KIND_DEPARTMENT_SCOPE
    # A profile competitor that Finance also lists keeps the stronger kind
    # and still routes to Finance; so does a profile vendor (profile kinds
    # outrank department kinds, so the vendor history keeps applying).
    assert vocab["acme corp"] == wp.VocabEntry(wp.KIND_COMPETITOR, "finance")
    stripe_vocab = wp.grounding_vocabulary(_profile(), [], [], [_department("finance", "Finance", watched=["Stripe"])])
    assert stripe_vocab["stripe"] == wp.VocabEntry(wp.KIND_VENDOR, "finance")
    # Two departments: the one supplying the winning kind owns the term.
    eng = _department("eng", "Engineering", scope=["Acme Migration"])
    fin = _department("finance", "Finance", watched=["Acme Migration"])
    both = wp.grounding_vocabulary(None, [], [], [eng, fin])
    assert both["acme migration"] == wp.VocabEntry(wp.KIND_DEPARTMENT_ENTITY, "finance")
    # Legacy plain-kind dicts still match.
    assert wp.match_entity("Brex Inc", vocab) == ("brex", wp.KIND_DEPARTMENT_ENTITY)
    assert wp.match_entity("Brex", {"brex": wp.KIND_VENDOR}) == ("brex", wp.KIND_VENDOR)
    assert wp.department_refs([_finance(head=7)]) == {"finance": wp.DepartmentRef("Finance", 7)}


def test_department_watched_entity_grounds_a_direct_add_and_routes(db: Path) -> None:
    d = wp.classify(_brex_proposal(), _brex_finding(), _dept_ctx())
    assert d.tier == wp.TIER_DIRECT and d.grounding_kind == wp.KIND_DEPARTMENT_ENTITY
    assert d.department == "finance" and "Finance watched entity" in d.reasons[0]
    out = wp.apply_proposals([_brex_proposal()], [_brex_finding()], _dept_ctx(), db_path=db)
    assert [o["outcome"] for o in out] == ["added"]
    row = ms.get_watchlist_item_by_slug("vendor-brex", db_path=db)
    assert row is not None and row.mode == MODE_ACTIVE
    assert row.route_to_department == "finance" and row.route_to_person_id == 7
    assert wp.policy_stamp_of(row)["department"] == "finance"


def test_department_scope_grounding_is_suggestion_only(db: Path) -> None:
    p = _proposal(slug="rss-expense", signal_type="rss", target="https://expensecards.example/feed",
                  grounding_entity="Expense card programs")
    f = _finding(title="Expense card programs shift", summary="New expense card programs launched.",
                 relevant_urls=["https://expensecards.example/feed"],
                 source_specialist="cfo,coo")
    d = wp.classify(p, f, _dept_ctx())
    assert d.tier == wp.TIER_SUGGEST and d.department == "finance"
    assert d.grounding_kind == wp.KIND_DEPARTMENT_SCOPE and "Finance scope item" in d.reasons[0]
    # Even on its own source a scope item cannot be direct.
    own = _proposal(slug="rss-programs", signal_type="rss", target="https://expense.example/feed",
                    grounding_entity="Expense card programs")
    own_f = _finding(title="programs", summary="s", relevant_urls=["https://expense.example/feed"],
                     source_specialist="cfo,coo")
    own_d = wp.classify(own, own_f, _dept_ctx())
    assert own_d.tier == wp.TIER_SUGGEST and any("grounded only in a department_scope" in r for r in own_d.reasons)
    out = wp.apply_proposals([p], [f], _dept_ctx(), db_path=db)
    assert out[0]["outcome"] == "suggested"
    row = ms.get_watchlist_item_by_slug("rss-expense", db_path=db)
    assert row is not None and row.mode == MODE_DRY_RUN and row.route_to_department == "finance"


def test_recent_decision_adds_a_point_and_a_routing_hint() -> None:
    decided = SimpleNamespace(summary="Evaluate Stripe as the EU payments vendor",
                              department="finance", timestamp="2026-09-01T00:00:00+00:00")
    p = _proposal(slug="vendor-stripe", signal_type="vendor_status", target="https://status.stripe.com",
                  grounding_entity="Stripe")
    f = _finding(title="Stripe incident", summary="Stripe API errors on 2026-09-01.",
                 relevant_urls=["https://status.stripe.com/"], confidence="medium")
    base = wp.classify(p, f, _dept_ctx())
    with_decision = wp.classify(p, f, _dept_ctx(recent_decisions=[decided]))
    assert with_decision.score == base.score + 1 and "named in a recent decision" in with_decision.reasons
    # Stripe is a profile vendor with no department; the decision's is used.
    assert base.department == "" and with_decision.department == "finance"
    # A decision for an unknown department is not a routing hint, and one
    # that names nothing relevant scores nothing.
    other = SimpleNamespace(summary="Move Stripe to annual billing", department="ops", timestamp="")
    assert wp.classify(p, f, _dept_ctx(recent_decisions=[other])).department == ""
    unrelated = SimpleNamespace(summary="Hire two SDRs in Q4", department="finance", timestamp="")
    assert wp.classify(p, f, _dept_ctx(recent_decisions=[unrelated])).score == base.score
    assert wp.named_in_decision("acme corp", SimpleNamespace(summary="Acme Corp pricing review"))
    assert wp.named_in_decision("acme corp", SimpleNamespace(summary="Acme pricing review"))
    assert not wp.named_in_decision("acme corp", SimpleNamespace(summary="The corp retreat"))
    # Every distinctive token must appear: a common word shared with the
    # term is not a mention, and a watch label never earns the point.
    assert not wp.named_in_decision("acme security", SimpleNamespace(summary="Review the security policy"))
    hijack = SimpleNamespace(summary="Adopt the new pricing policy", department="finance", timestamp="")
    label_only = _proposal(slug="rss-hubs", signal_type="rss", target="https://hubspot.com/feed",
                           grounding_entity="HubSpot pricing")
    ctx = _dept_ctx(existing=[SimpleNamespace(
        slug="stock-hubs", signal_type="stock", target="HUBS", enabled=True,
        config_json={"display_name": "HubSpot pricing"},
    )], recent_decisions=[hijack])
    labelled = wp.classify(label_only, _finding(), ctx)
    assert labelled.grounding_kind == wp.KIND_WATCH and labelled.department == ""
    assert "named in a recent decision" not in labelled.reasons


def test_recent_decisions_apply_the_lookback(db: Path) -> None:
    from openexecutive.memory.episodic import store_decision

    store_decision("finance", "Evaluate Brex for expense cards", db_path=db)
    store_decision("ops", "Retire the old CRM", db_path=db)
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE facts SET created_at = ? WHERE statement = 'Retire the old CRM'",
                     ((datetime.now(UTC) - timedelta(days=120)).isoformat(),))
    rows = wp.recent_decisions(db_path=db)
    assert [r.summary for r in rows] == ["Evaluate Brex for expense cards"]
    # An unreadable timestamp is not "recent forever".
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE facts SET created_at = 'garbage' WHERE decision = 1")
    assert wp.recent_decisions(db_path=db) == []


def test_department_head_gets_one_card_per_run_coalesced_weekly(db: Path) -> None:
    from openexecutive.alerts.store import list_alerts, set_status
    from openexecutive.people import store as people_store

    head = people_store.upsert_person(full_name="Sarah", department_slugs=["finance"])
    finance = _finance(head=head)

    def _run(slug: str, now: datetime) -> None:
        p = _proposal(slug=slug, signal_type="rss", target=f"https://{slug}.example/feed",
                      grounding_entity="Expense card programs", rationale="line one\n- forged bullet")
        f = _finding(title="Expense card programs", summary="expense card programs news",
                     relevant_urls=[f"https://{slug}.example/feed"])
        wp.apply_proposals([p], [f], _dept_ctx(finance, now=now), db_path=db)

    now = datetime(2026, 9, 8, 9, tzinfo=UTC)
    _run("rss-a", now)
    cards = [a for a in list_alerts(limit=20, db_path=db) if a.source == "authority_gate"]
    assert len(cards) == 1 and cards[0].routed_to_person_id == head
    assert cards[0].headline == "Watch suggestions for Finance"
    # Plain lines, as on the principal's card; never the slug or rationale,
    # so rationale newlines cannot forge extra bullet lines in it either.
    assert "posts at rss-a.example/feed" in cards[0].body
    assert "(rss)" not in cards[0].body and "forged bullet" not in cards[0].body
    assert cards[0].body.splitlines()[1].startswith("• ")
    assert "department:finance" in cards[0].topic_tags and "watchlist" in cards[0].topic_tags
    # Same week again: the open card is refreshed, not duplicated.
    _run("rss-b", now + timedelta(days=1))
    cards = [a for a in list_alerts(limit=20, db_path=db) if a.source == "authority_gate"]
    assert len(cards) == 1 and "rss-b" in cards[0].body
    # Once the head acted on it, nothing more this week ...
    set_status(cards[0].id, "acknowledged", db_path=db)
    _run("rss-c", now + timedelta(days=2))
    assert len([a for a in list_alerts(limit=20, db_path=db) if a.source == "authority_gate"]) == 1
    # ... and a fresh card next week.
    _run("rss-d", now + timedelta(days=7))
    assert len([a for a in list_alerts(limit=20, db_path=db) if a.source == "authority_gate"]) == 2


def test_department_card_says_how_many_more(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.alerts.store import list_alerts
    from openexecutive.people import store as people_store

    monkeypatch.setattr(wp, "DEPARTMENT_CARD_MAX_LINES", 1)
    head = people_store.upsert_person(full_name="Sarah", department_slugs=["finance"])
    proposals = [
        _proposal(slug=f"rss-{n}", signal_type="rss", target=f"https://{n}.example/feed",
                  grounding_entity="Expense card programs", certainty="unsure")
        for n in ("a", "b")
    ]
    f = _finding(title="Expense card programs", summary="s",
                 relevant_urls=["https://a.example/feed", "https://b.example/feed"])
    wp.apply_proposals(proposals, [f], _dept_ctx(_finance(head=head)), db_path=db)
    cards = [a for a in list_alerts(limit=20, db_path=db) if a.source == "authority_gate"]
    assert len(cards) == 1
    assert cards[0].body.splitlines()[-1] == "• and 1 more on the Watch list"


def test_department_card_falls_back_to_the_principal(db: Path) -> None:
    from openexecutive.alerts.store import list_alerts
    from openexecutive.people import store as people_store

    principal = people_store.upsert_person(full_name="Jo", is_principal=True)
    p = _proposal(slug="rss-x", signal_type="rss", target="https://x.example/feed",
                  grounding_entity="Expense card programs")
    f = _finding(title="Expense card programs", summary="s", relevant_urls=["https://x.example/feed"])
    wp.apply_proposals([p], [f], _dept_ctx(_finance(head=None)), db_path=db)
    cards = [a for a in list_alerts(limit=20, db_path=db) if a.source == "authority_gate"]
    assert len(cards) == 1 and cards[0].routed_to_person_id == principal
    row = ms.get_watchlist_item_by_slug("rss-x", db_path=db)
    assert row is not None and row.route_to_department == "finance" and row.route_to_person_id is None


def test_nudge_counts_only_suggestions_no_head_received(db: Path) -> None:
    from openexecutive.alerts.store import list_alerts

    for i in range(3):
        ms.insert_watchlist_item(
            slug=f"rss-d{i}", signal_type="rss", target=f"https://d{i}.com/feed",
            mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, route_to_department="finance",
            route_to_person_id=7, db_path=db,
        )
        _backdate(db, f"rss-d{i}", 8)
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 0
    assert not [a for a in list_alerts(limit=10, db_path=db) if a.source == wp.NUDGE_ALERT_SOURCE]
    # A department without a head is still the principal's pile: those rows
    # count, so they can never become invisible.
    for i in range(3):
        ms.insert_watchlist_item(
            slug=f"rss-n{i}", signal_type="rss", target=f"https://n{i}.com/feed",
            mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, route_to_department="ops",
            db_path=db,
        )
        _backdate(db, f"rss-n{i}", 8)
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 1


def test_a_no_to_the_card_declines_what_it_lists(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Rufus's 11-09 1:1: "No, I don't" cleared the card, but the Watch list
    # still asked about all five.
    from openexecutive.alerts import lifecycle
    from openexecutive.alerts.store import get_alert, list_alerts

    for i in range(3):
        ms.insert_watchlist_item(
            slug=f"rss-p{i}", signal_type="rss", target=f"https://p{i}.com/feed",
            config={wp.CARD_LINE_KEY: f"News from P{i}"},
            mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
        )
        _backdate(db, f"rss-p{i}", 8)
    # A department head's suggestion isn't on the principal's card.
    ms.insert_watchlist_item(
        slug="rss-head", signal_type="rss", target="https://head.com/feed",
        mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, route_to_department="finance",
        route_to_person_id=7, db_path=db,
    )
    assert wp.sweep(datetime.now(UTC), db_path=db)["nudged"] == 1
    card = next(a for a in list_alerts(limit=10, db_path=db) if a.source == wp.NUDGE_ALERT_SOURCE)
    # Filed after the card was written: never in front of them, so it stays.
    ms.insert_watchlist_item(
        slug="rss-new", signal_type="rss", target="https://new.com/feed",
        mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
    )
    # Filed later with the same line as one on the card: still unseen.
    ms.insert_watchlist_item(
        slug="rss-p0-blog", signal_type="rss", target="https://p0.com/blog",
        config={wp.CARD_LINE_KEY: "News from P0"},
        mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
    )
    assert wp.decline_card_suggestions(card, db_path=db) == 3
    assert {i.slug for i in ms.list_pending_suggestions(db_path=db)} == {"rss-head", "rss-new", "rss-p0-blog"}
    assert not ms.is_declined("https://new.com/feed", db_path=db)
    # Remembered: the research pass won't propose them again.
    assert ms.is_declined("https://p0.com/feed", db_path=db)
    # Any other card: nothing.
    other = get_alert(card.id, db_path=db).model_copy(update={"source": "alert_review"})
    assert wp.decline_card_suggestions(other, db_path=db) == 0
    # Dismissing a card, by its button, ack_alert or answer_card, runs it.
    seen: list[Any] = []
    monkeypatch.setattr(wp, "decline_card_suggestions", lambda alert, **_k: seen.append(alert) or 0)
    lifecycle.record_ack_feedback(card, "acknowledged")
    lifecycle.record_ack_feedback(card, "dismissed")
    # A bulk sweep is housekeeping, not a no.
    lifecycle.record_ack_feedback(card, "dismissed", decline_suggestions=False)
    assert seen == [card]


def test_a_no_to_a_department_card_declines_only_that_departments(db: Path) -> None:
    for slug, dept in (("rss-f", "finance"), ("rss-o", "ops")):
        ms.insert_watchlist_item(
            slug=slug, signal_type="rss", target=f"https://{slug}.com/feed",
            mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, route_to_department=dept,
            route_to_person_id=7, db_path=db,
        )
    from openexecutive.departments.authority import PROPOSAL_ALERT_SOURCE

    shown = next(i for i in ms.list_pending_suggestions(db_path=db) if i.slug == "rss-f")
    body = f"I'd like to keep an eye on these for Finance.\n• {wp.card_line(shown)}"
    ms.insert_watchlist_item(
        slug="rss-f2", signal_type="rss", target="https://rss-f2.com/feed",
        mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, route_to_department="finance",
        route_to_person_id=7, db_path=db,
    )
    card = SimpleNamespace(source=PROPOSAL_ALERT_SOURCE, topic_tags=["department:finance", "watchlist"], body=body)
    assert wp.decline_card_suggestions(card, db_path=db) == 1
    # Only the one the card names: rss-f2 came later, rss-o is another department's.
    assert {i.slug for i in ms.list_pending_suggestions(db_path=db)} == {"rss-o", "rss-f2"}
    # A department's card that isn't about watches: nothing.
    other = SimpleNamespace(source=PROPOSAL_ALERT_SOURCE, topic_tags=["department:ops"], body=body)
    assert wp.decline_card_suggestions(other, db_path=db) == 0


# --------------------------------------------------------------------- #
# Profile entry parsing, short names, finding auto-link
# --------------------------------------------------------------------- #


def test_entity_names_parse_profile_entries() -> None:
    assert wp.entity_names("Tesla (TSLA) — Model Y is the volume benchmark") == (["Tesla"], ["TSLA"], [])
    assert wp.entity_names("GM / Chevrolet (Equinox EV, Blazer EV) — direct competitor") == (["GM", "Chevrolet"], [], [])
    assert wp.entity_names("BYD — global cost leader; tariff-gated out of the US") == (["BYD"], [], [])
    assert wp.entity_names("Hyundai / Kia (Ioniq 5, EV6, EV9) - strong value") == (["Hyundai", "Kia"], [], [])
    assert wp.entity_names("BYD (1211.HK)") == (["BYD"], ["1211.HK"], [])
    assert wp.entity_names("Stripe: payments") == (["Stripe"], [], [])
    assert wp.entity_names("Brex (brex.com, status.brex.com) — expense cards") == (["Brex"], [], ["brex.com", "status.brex.com"])
    # A parenthesised list, or a product name, is never a ticker; hyphenated
    # names survive; whitespace and length are bounded.
    assert wp.entity_names("Apple (IOS, MAC) — CarPlay") == (["Apple"], [], [])
    assert wp.entity_names("Mercedes-Benz (MBG.DE)") == (["Mercedes-Benz"], ["MBG.DE"], [])
    assert wp.entity_names("Acme" + " " * 100000 + "Corp — x").names == ["Acme Corp"]
    assert wp.entity_names("  ") == ([], [], [])
    # Dashes without spaces still split; ordinary abbreviations are not
    # tickers; lower-case tickers are upper-cased; a URL entry grounds as its
    # site label with the host pinned; nested parentheses are stripped.
    assert wp.entity_names("Tesla (TSLA)—Model Y is the volume benchmark") == (["Tesla"], ["TSLA"], [])
    assert wp.entity_names("Stripe (US, EU) — payments").tickers == []
    assert wp.entity_names("Gartner (IT) — research").tickers == []
    assert wp.entity_names("Tesla (tsla)") == (["Tesla"], [], [])  # tickers are typed in capitals
    # Only the name part's parentheses are read: description text can
    # neither pin a domain nor mint a ticker; a capitalised brand or a
    # product name is never a ticker; one-letter tickers and a ticker
    # beside a host both work; text after a ticker symbol is a note.
    assert wp.entity_names("BYD — cost leader (byd-watch.com)") == (["BYD"], [], [])
    assert wp.entity_names("Tesla — the (BEV) leader").tickers == []
    assert wp.entity_names("Alphabet (Waymo) — self-driving") == (["Alphabet"], [], [])
    assert wp.entity_names("Ford (F) — legacy OEM") == (["Ford"], ["F"], [])
    assert wp.entity_names("Acme Corp (ACME, acme.com)") == (["Acme Corp"], ["ACME"], ["acme.com"])
    assert wp.entity_names("GM (GM.COM)") == (["GM"], [], ["gm.com"])  # a host typed in capitals
    assert wp.entity_names("GM (gm.com.)") == (["GM"], [], ["gm.com"])
    assert wp.entity_names("Apple (Siri) — assistant") == (["Apple"], [], [])
    assert wp.entity_names("Acme (Mach-E) - EVs") == (["Acme"], [], [])
    assert wp.ticker_symbol("NVDA — our supplier") == "NVDA"
    assert wp.ticker_symbol("1211.HK (BYD)") == "1211.HK"
    profile = CompanyProfile.model_validate({"name": "X", "tickers": ["NVDA — our supplier", "f"]})
    tick_vocab = wp.grounding_vocabulary(profile, [], [])
    assert wp.match_entity("NVDA", tick_vocab) == ("nvda", wp.KIND_TICKER)
    assert wp.match_entity("F", tick_vocab) == ("f", wp.KIND_TICKER)
    assert wp.entity_names("https://status.stripe.com") == (["stripe"], [], ["status.stripe.com"])
    assert wp.entity_names("GM (Chevrolet (EV) brand)") == (["GM"], [], [])
    assert wp.entity_names("Stripe (stripe.com/blog; payments)").names == ["Stripe"]


def _descriptive_profile() -> CompanyProfile:
    return CompanyProfile.model_validate({
        "name": "Halcyon Motors",
        "competitive_landscape": {"primary_competitors": [
            "Tesla (TSLA) — Model Y is the volume benchmark and price-cut pace-setter",
            "BYD — global cost leader; tariff-gated out of the US for now",
            "GM / Chevrolet (Equinox EV, Blazer EV) — direct mainstream-price competitor",
            "Hyundai / Kia (Ioniq 5, EV6, EV9) — strong value + charging speed",
        ]},
        "vendors": ["CATL — sole LFP cell source"],
        "tickers": ["1211.HK"],
    })


def test_descriptive_profile_entries_ground_by_name_only() -> None:
    vocab = wp.grounding_vocabulary(_descriptive_profile(), [], [])
    assert vocab["tesla"].kind == wp.KIND_COMPETITOR and vocab["tsla"].kind == wp.KIND_TICKER
    assert vocab["byd"].kind == wp.KIND_COMPETITOR and vocab["gm"].kind == wp.KIND_COMPETITOR
    assert vocab["chevrolet"].kind == wp.KIND_COMPETITOR and vocab["kia"].kind == wp.KIND_COMPETITOR
    assert vocab["catl"].kind == wp.KIND_VENDOR and vocab["1211 hk"].kind == wp.KIND_TICKER
    # The description never becomes a term, so "Model Y" or "global" ground nothing.
    assert wp.match_entity("Model Y", vocab) is None and wp.match_entity("global cost leader", vocab) is None
    # Short names match as whole words, in longer forms too.
    assert wp.match_entity("BYD", vocab) == ("byd", wp.KIND_COMPETITOR)
    assert wp.match_entity("BYD Auto", vocab) == ("byd", wp.KIND_COMPETITOR)
    assert wp.match_entity("GM", vocab) == ("gm", wp.KIND_COMPETITOR)
    assert wp.match_entity("General Motors", vocab) is None  # a different name is not a match
    # A ticker grounds only as itself: never as part of a longer name.
    assert wp.match_entity("TSLA", vocab) == ("tsla", wp.KIND_TICKER)
    assert wp.match_entity("TSLA Holdings", vocab) is None
    assert wp.match_entity("1211.HK", vocab) == ("1211 hk", wp.KIND_TICKER)


def test_short_names_get_own_source_and_finding_support() -> None:
    profile = _descriptive_profile()
    vocab = wp.grounding_vocabulary(profile, [], [])
    ctx = wp.PolicyContext(vocabulary=vocab, priority_terms=[], existing=[])
    f = _finding(title="BYD enters Mexico", summary="BYD launched the Seal in Mexico on 2026-09-05.",
                 relevant_urls=["https://www.byd.com/news/mexico"], source_specialist="cso")
    p = _proposal(slug="rss-byd", target="https://www.byd.com/news/feed", grounding_entity="BYD")
    d = wp.classify(p, f, ctx)
    assert d.tier == wp.TIER_DIRECT and "target is the entity's own source" in d.reasons
    # A look-alike registration carries the same site label, so it passes the
    # label rule — pinning the entity's domains in the profile closes that.
    look_alike = _proposal(slug="rss-byd-fake", target="https://byd.co.ke/feed", grounding_entity="BYD")
    fake_f = f.model_copy(update={"relevant_urls": ["https://byd.co.ke/news"]})
    assert wp.classify(look_alike, fake_f, ctx).tier == wp.TIER_DIRECT
    pinned = CompanyProfile.model_validate({
        "name": "X", "competitive_landscape": {"primary_competitors": ["BYD (byd.com) — cost leader"]},
    })
    pinned_ctx = wp.PolicyContext(vocabulary=wp.grounding_vocabulary(pinned, [], []), priority_terms=[], existing=[])
    assert pinned_ctx.vocabulary["byd"].domains == ("byd.com",)
    fake_d = wp.classify(look_alike, fake_f, pinned_ctx)
    assert fake_d.tier == wp.TIER_SUGGEST and "target is the entity's own source" not in fake_d.reasons
    assert wp.classify(p, f, pinned_ctx).tier == wp.TIER_DIRECT
    # A subdomain of a pinned host is the entity's; a host that merely ends
    # in the pinned name is not.
    sub = _proposal(slug="rss-byd-news", target="https://news.byd.com/feed", grounding_entity="BYD")
    assert wp._is_own_source(sub, "byd", pinned_ctx.vocabulary)
    evil = _proposal(slug="rss-byd-evil", target="https://byd.com.evil.example/feed", grounding_entity="BYD")
    assert not wp._is_own_source(evil, "byd", pinned_ctx.vocabulary)
    # A dotted ticker: own source when grounded as the ticker, and named in
    # text as a phrase.
    f2 = _finding(title="BYD (1211.HK) slides", summary="1211.HK fell 6% on 2026-09-05.", relevant_urls=[])
    stock = _proposal(slug="stock-byd", signal_type="stock", target="1211.HK", grounding_entity="1211.HK")
    d2 = wp.classify(stock, f2, ctx)
    assert d2.tier == wp.TIER_DIRECT and d2.grounding_kind == wp.KIND_TICKER
    # Grounded as the competitor name instead, the ticker is not its own
    # source, so it is a suggestion.
    assert wp.classify(_proposal(slug="stock-byd2", signal_type="stock", target="1211.HK",
                                 grounding_entity="BYD"), f2, ctx).tier == wp.TIER_SUGGEST
    decided = SimpleNamespace(summary="Benchmark C1 pricing against BYD monthly", department="", timestamp="")
    assert wp.named_in_decision("byd", decided, vocab)
    # A short name inside a longer term needs the vocabulary to count.
    two_word = SimpleNamespace(summary="Benchmark against BYD Auto monthly", department="", timestamp="")
    assert wp.named_in_decision("byd auto", two_word, vocab)
    # Every distinctive word must appear: "BYD Auto" is not named by "BYD pricing".
    assert not wp.named_in_decision("byd auto", SimpleNamespace(summary="Watch BYD pricing"), vocab)
    assert wp.entity_declined("BYD Auto", [SimpleNamespace(reason="not_relevant", entity="BYD")])
    assert not wp.entity_declined("Rivian", [SimpleNamespace(reason="not_relevant", entity="BYD")])
    # A generic declined word never blocks, and a longer declined name does
    # not block the short profile name inside it.
    assert not wp.entity_declined("Acme Data Systems", [SimpleNamespace(reason="not_relevant", entity="data")])
    assert not wp.entity_declined("GM", [SimpleNamespace(reason="not_relevant", entity="GM Financial")])


def test_proposal_without_finding_index_is_linked_to_the_citing_finding(db: Path) -> None:
    findings = [
        _finding(title="Unrelated", summary="Globex raised prices.", relevant_urls=["https://globex.com/blog"]),
        _finding(title="Acme cut prices", summary="Acme Corp cut list prices 20%.",
                 relevant_urls=["https://www.acme.com/blog/pricing"]),
        _finding(title="ACME slides", summary="ACME fell 8% on 2026-09-01.", relevant_urls=[]),
    ]
    ctx = _ctx()
    unlinked = _proposal(finding_index=None)
    assert wp.auto_link_finding(unlinked, findings, ctx) == 1
    stock = _proposal(slug="stock-acme", signal_type="stock", target="ACME", grounding_entity="ACME",
                      finding_index=None)
    assert wp.auto_link_finding(stock, findings, ctx) == 1  # names the entity first
    nowhere = _proposal(slug="rss-else", target="https://elsewhere.com/feed", grounding_entity="Acme Corp",
                        finding_index=None)
    assert wp.auto_link_finding(nowhere, findings, ctx) is None
    # A guessed index that does not concern the source is repaired too.
    wrong = _proposal(finding_index=0)
    out = wp.apply_proposals([unlinked, nowhere, wrong], findings, ctx, db_path=db)
    assert [o["outcome"] for o in out] == ["added", "rejected", "rejected"]  # 3rd: same source as 1st
    assert "duplicate target" in out[2]["result_preview"]
    row = ms.get_watchlist_item_by_slug("rss-acme-blog", db_path=db)
    assert row is not None
    assert wp.policy_stamp_of(row)["reasons"][0] == "linked to finding #2, which concerns this source"
    assert wp.policy_stamp_of(row)["specialist"] == "cso"
    assert wp.policy_stamp_of(row)["source_url"] == "https://www.acme.com/blog/pricing"
    assert unlinked.finding_index is None  # the caller's proposal is left as filed
    repaired = wp.apply_proposals([_proposal(slug="rss-acme-2", target="https://acme.com/news/feed",
                                             finding_index=0)], findings, ctx, db_path=db)
    assert repaired[0]["outcome"] == "added"
    row2 = ms.get_watchlist_item_by_slug("rss-acme-2", db_path=db)
    assert row2 is not None and wp.policy_stamp_of(row2)["reasons"][0].endswith("(the cited #1 did not)")
    # A cited finding that does not concern the source, with nothing better
    # to link, still makes the proposal a suggestion (it came from research)
    # but is never the evidence link shown beside Approve.
    stray = wp.apply_proposals([_proposal(slug="rss-stray", target="https://elsewhere.com/feed",
                                          grounding_entity="Acme Corp", finding_index=0)], findings, ctx, db_path=db)
    assert stray[0]["outcome"] == "suggested"
    stray_row = ms.get_watchlist_item_by_slug("rss-stray", db_path=db)
    assert stray_row is not None and wp.policy_stamp_of(stray_row)["source_url"] == ""


def test_auto_link_prefers_the_finding_that_cites_the_source() -> None:
    ctx = _ctx()
    findings = [
        _finding(title="Globex beats, Acme called a laggard", summary="Acme lags Globex; Initech too.",
                 relevant_urls=["https://globex.com/ir/q3"],
                 source_specialist="cfo,cso"),
        _finding(title="Acme Q3", summary="ACME reported Q3.", relevant_urls=["https://acme.com/ir/q3"],
                 confidence="medium"),
    ]
    stock = _proposal(slug="stock-acme", signal_type="stock", target="ACME", grounding_entity="ACME",
                      finding_index=None)
    assert wp.auto_link_finding(stock, findings, ctx) == 1  # names the ticker, not just the entity
    # With the entity's domains pinned, only a finding citing those sites
    # counts as "the entity's site" — a look-alike page cannot shop for the
    # evidence slot.
    pinned = CompanyProfile.model_validate({"name": "X", "competitive_landscape": {
        "primary_competitors": ["GM (gm.com)"]}})
    pctx = wp.PolicyContext(vocabulary=wp.grounding_vocabulary(pinned, [], []), priority_terms=[], existing=[])
    gm_findings = [
        _finding(title="GM recall", summary="GM recalled the Blazer EV.", relevant_urls=["https://news.gm.com/x"],
                 confidence="medium"),
        _finding(title="GM news", summary="GM slashes prices.", relevant_urls=["https://gm.co.ke/news"],
                 source_specialist="cso,cfo"),
    ]
    gm = _proposal(slug="stock-gm", signal_type="stock", target="GM", grounding_entity="GM", finding_index=None)
    assert wp.auto_link_finding(gm, gm_findings, pctx) == 0
    feed = _proposal(slug="rss-acme-ir", target="https://acme.com/ir/feed", grounding_entity="Acme Corp",
                     finding_index=None)
    assert wp.auto_link_finding(feed, findings, ctx) == 1  # cites the site
    # An entity outside company data never links by name (classify would
    # not honour it either); a cited site still does.
    stranger = _proposal(slug="stock-intc", signal_type="stock", target="INTC", grounding_entity="Initech Holdings",
                         finding_index=None)
    assert wp.auto_link_finding(stranger, findings, ctx) is None


def test_budgets_go_to_the_strongest_proposals_not_the_earliest(db: Path) -> None:
    """The dev run that motivated this: two score-3 suggestions filed first
    took the whole suggestion budget, and a score-4 Finance watched-entity
    proposal filed third was rejected as over budget. Proposals are now
    applied strongest first; summaries keep file order; ties keep file
    order (the first-filed of the two score-3 proposals survives)."""
    ctx = _dept_ctx(settings=wp.PolicySettings(max_direct_adds=2, max_suggestions=2))
    # Two query watches (always suggestions) grounded in a competitor and a
    # vendor: score 3 each. Brex on its own status page, a Finance watched
    # entity, high-confidence finding: score 4, marked unsure.
    weak_a = _proposal(slug="q-globex", signal_type="query", target="Globex pricing moves",
                       grounding_entity="Globex", finding_index=0)
    weak_b = _proposal(slug="q-stripe", signal_type="query", target="Stripe outage history",
                       grounding_entity="Stripe", finding_index=1)
    strong = _brex_proposal(finding_index=2, certainty="unsure")
    findings = [
        _finding(title="Globex cuts prices", summary="Globex cut list prices 10%.", confidence="high",
                 relevant_urls=["https://globex.com/pricing"]),
        _finding(title="Stripe outage", summary="Stripe had a two-hour outage.", confidence="high",
                 relevant_urls=["https://status.stripe.com/incidents/9"]),
        _brex_finding(),
    ]
    out = wp.apply_proposals([weak_a, weak_b, strong], findings, ctx, db_path=db)
    assert [(o["slug"], o["outcome"]) for o in out] == [
        ("q-globex", "suggested"), ("q-stripe", "rejected"), (strong.slug, "suggested"),
    ]
    assert "suggestion budget spent this run" in out[1]["result_preview"]


def test_duplicate_target_keeps_its_strongest_filing(db: Path) -> None:
    weak = _proposal(slug="rss-weak", grounding_entity="Initech")  # not in company data
    strong = _proposal(slug="rss-strong", grounding_entity="Acme Corp")  # same target, direct
    out = wp.apply_proposals([weak, strong], [_finding()], _ctx(), db_path=db)
    assert [(o["slug"], o["outcome"]) for o in out] == [("rss-weak", "rejected"), ("rss-strong", "added")]
    assert "duplicate target" in out[0]["result_preview"]


def test_a_direct_add_outranks_a_higher_scoring_suggestion_for_the_last_slot(db: Path) -> None:
    """Score is not the whole story: a standing query can outscore a direct
    add but can only ever be a suggestion, so at the enabled-watch ceiling
    the live add must take the slot."""
    ctx = _ctx(settings=wp.PolicySettings(max_enabled=1, max_direct_adds=2, max_suggestions=2),
               recent_decisions=[SimpleNamespace(summary="Globex pricing review", department="")])
    direct = _proposal(slug="rss-acme-blog", finding_index=1)
    query = _proposal(slug="q-globex", signal_type="query", target="Globex pricing moves",
                      grounding_entity="Globex", finding_index=0)
    findings = [
        _finding(title="Globex cuts prices", summary="Globex cut list prices 10%.", confidence="high",
                 source_specialist="cso,cfo"),
        _finding(),
    ]
    out = wp.apply_proposals([direct, query], findings, ctx, db_path=db)
    assert [(o["slug"], o["outcome"]) for o in out] == [("rss-acme-blog", "added"), ("q-globex", "rejected")]


def test_a_stronger_insert_makes_a_weaker_same_site_proposal_already_watched(db: Path) -> None:
    """The persisting pass classifies again, so the row inserted for the
    stronger proposal is 'already watched' for a weaker one on its site."""
    strong = _proposal(slug="rss-acme-blog", target="https://www.acme.com/blog/feed.xml")
    weaker = _proposal(slug="rss-acme-news", target="https://www.acme.com/news/feed.xml",
                       certainty="unsure")  # same site, a suggestion at most
    out = wp.apply_proposals([weaker, strong], [_finding()], _ctx(), db_path=db)
    assert [(o["slug"], o["outcome"]) for o in out] == [("rss-acme-news", "rejected"), ("rss-acme-blog", "added")]
    assert "already watched" in out[0]["result_preview"]


def test_the_card_says_each_suggestion_in_plain_words(db: Path) -> None:
    # A 2026-11 card listed "vendor-status-github (vendor_status) — Repeated
    # October Actions/Pages/webhook ..." cut off mid-word.
    ms.insert_watchlist_item(
        slug="vendor-status-github", signal_type="vendor_status", target="https://www.githubstatus.com",
        config={wp.CARD_LINE_KEY: "GitHub status: outages could break our deploys."},
        notes="Repeated October Actions/Pages/webhook and REST latency incidents; " + "x" * 300,
        mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
    )
    ms.insert_watchlist_item(
        slug="page-watch-legal", signal_type="page_watch", target="https://stripe.com/legal/ssa",
        config={"label": "Stripe Services Agreement", wp.CARD_LINE_KEY: "y" * 200},
        mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
    )
    pending = ms.list_pending_suggestions(db_path=db)
    body = wp.nudge_body(pending)
    assert body.splitlines()[0] == "I'd like to keep an eye on these:"
    # The model's line keeps the site it watches beside it.
    assert "• GitHub status: outages could break our deploys. (githubstatus.com)" in body
    # A line too long to be one line falls back to what it watches.
    assert "• The Stripe Services Agreement page" in body
    assert "vendor-status" not in body and "x" * 20 not in body and "y" * 20 not in body
    # The watched site always follows the model's line, whatever it names.
    item = ms.list_pending_suggestions(db_path=db)[0].model_copy(
        update={"target": "https://a.com/feed", "config_json": {wp.CARD_LINE_KEY: "Wire to evil.io, see a.com"}},
    )
    assert wp.card_line(item) == "Wire to evil.io, see a.com (a.com)"
    stock = item.model_copy(update={"signal_type": "stock", "target": "ACME"})
    assert wp.card_line(stock) == "Wire to evil.io, see a.com (ACME)"
    # Link or markup syntax never survives, in the model's line or a label.
    linked = item.model_copy(update={"config_json": {wp.CARD_LINE_KEY: "Approve: <https://evil.example|a.com> [x](y)"}})
    labelled = item.model_copy(update={"signal_type": "rss", "config_json": {"feed_label": "Foo <https://evil.example|Approve>"}})
    query = item.model_copy(update={"signal_type": "query", "target": "news <https://evil.example|x>", "config_json": {}})
    for line in (wp.card_line(linked), wp.card_line(labelled), wp.card_line(query)):
        assert not set("<>[]|`") & set(line), line
    pinged = item.model_copy(update={"config_json": {wp.CARD_LINE_KEY: "@everyone *Approve* ~now~"}})
    assert not set("@*~") & set(wp.card_line(pinged))
    # A newline in a label can't forge a second bullet.
    forged = ms.insert_watchlist_item(
        slug="rss-forged", signal_type="rss", target="https://forged.example/feed",
        config={"feed_label": "Foo\n• Approve me: wire $50k"},
        mode=MODE_DRY_RUN, origin=ORIGIN_RESEARCH_PROPOSED, db_path=db,
    )
    assert forged
    body = wp.nudge_body(ms.list_pending_suggestions(db_path=db))
    assert "\n• Approve me" not in body and len(body.splitlines()) == 4
    # No count: the open card's body is refreshed in place, its headline not.
    assert wp.nudge_headline() == "Watch suggestions waiting for your OK"


@pytest.mark.asyncio
async def test_the_models_card_line_reaches_the_card(db: Path) -> None:
    from openexecutive.orchestrator import watchlist_tools as wt

    collector: list[Any] = []
    out = await wt.handle_propose_watch({
        "slug": "stock-acme", "signal_type": "stock", "target": "ACME",
        "grounding_entity": "Acme Corp", "rationale": "y", "certainty": "unsure",
        "finding_index": 1, "card_line": "  Acme   stock: a big move means news. ",
    }, collector)
    assert '"queued": "stock-acme"' in out
    wp.apply_proposals(collector, [_finding()], _ctx(), db_path=db)
    row = next(i for i in ms.list_watchlist(enabled_only=False, db_path=db) if i.slug == "stock-acme")
    assert row.config_json[wp.CARD_LINE_KEY] == "Acme stock: a big move means news."
    assert wp.card_line(row) == "Acme stock: a big move means news. (ACME)"
    # An over-long line is dropped, never cut mid-word.
    await wt.handle_propose_watch({
        "slug": "stock-acmf", "signal_type": "stock", "target": "ACMF",
        "grounding_entity": "Acme Corp", "rationale": "y", "certainty": "unsure",
        "finding_index": 1, "card_line": "word " * 40,
    }, collector)
    assert collector[-1].card_line == ""
