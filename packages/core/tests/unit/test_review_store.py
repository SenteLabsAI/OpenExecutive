"""Unit tests for the SME knowledge review store."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from openexecutive.knowledge.review_store import (
    Annotation,
    ContentType,
    Priority,
    ReviewItem,
    ReviewStatus,
    ReviewStore,
)


@pytest.fixture()
def store(tmp_path: Path) -> ReviewStore:
    db = tmp_path / "review.db"
    ReviewStore.initialize_db(db)
    return ReviewStore(db_path=db)


def _register_builtin(store: ReviewStore, domain: str = "finance", filename: str = "ratios.md") -> ReviewItem:
    item_id = f"builtin:{domain}:{filename}"
    return store.register(
        item_id=item_id,
        content_type=ContentType.BUILTIN,
        domain=domain,
        filename=filename,
    )


# ---------------------------------------------------------------------------
# Register / idempotency
# ---------------------------------------------------------------------------


def test_register_creates_pending_item(store: ReviewStore) -> None:
    item = _register_builtin(store)
    assert item.status == ReviewStatus.PENDING
    assert item.priority == Priority.NORMAL
    assert item.content_type == ContentType.BUILTIN


def test_register_is_idempotent(store: ReviewStore) -> None:
    first = _register_builtin(store)
    # Approve, then re-register — should not reset the status
    store.set_status(first.item_id, ReviewStatus.APPROVED)
    second = store.register(
        item_id=first.item_id,
        content_type=ContentType.BUILTIN,
        domain="finance",
        filename="ratios.md",
    )
    assert second.status == ReviewStatus.APPROVED


# ---------------------------------------------------------------------------
# touch_modified transitions
# ---------------------------------------------------------------------------


def test_touch_modified_approved_becomes_needs_revision(store: ReviewStore) -> None:
    item = _register_builtin(store)
    store.set_status(item.item_id, ReviewStatus.APPROVED)
    store.touch_modified(item.item_id)
    updated = store.get_item(item.item_id)
    assert updated is not None
    assert updated.status == ReviewStatus.NEEDS_REVISION


def test_touch_modified_rejected_becomes_needs_revision(store: ReviewStore) -> None:
    item = _register_builtin(store)
    store.set_status(item.item_id, ReviewStatus.REJECTED)
    store.touch_modified(item.item_id)
    updated = store.get_item(item.item_id)
    assert updated is not None
    assert updated.status == ReviewStatus.NEEDS_REVISION


def test_touch_modified_pending_stays_pending(store: ReviewStore) -> None:
    item = _register_builtin(store)
    store.touch_modified(item.item_id)
    updated = store.get_item(item.item_id)
    assert updated is not None
    assert updated.status == ReviewStatus.PENDING


def test_touch_modified_needs_revision_stays(store: ReviewStore) -> None:
    item = _register_builtin(store)
    store.set_status(item.item_id, ReviewStatus.NEEDS_REVISION)
    store.touch_modified(item.item_id)
    updated = store.get_item(item.item_id)
    assert updated is not None
    assert updated.status == ReviewStatus.NEEDS_REVISION


# ---------------------------------------------------------------------------
# get_withheld_keys
# ---------------------------------------------------------------------------


def test_get_withheld_keys_covers_rejected_and_pending(store: ReviewStore) -> None:
    item_a = _register_builtin(store, filename="ratios.md")
    item_b = _register_builtin(store, filename="fundraising.md")
    _register_builtin(store, filename="modeling.md")  # left pending

    store.set_status(item_a.item_id, ReviewStatus.REJECTED)
    store.set_status(item_b.item_id, ReviewStatus.APPROVED)

    withheld = store.get_withheld_keys(ContentType.BUILTIN)
    assert withheld == {("finance", "ratios.md"), ("finance", "modeling.md")}
    assert ("finance", "fundraising.md") not in withheld


def test_get_withheld_keys_empty_when_nothing_withheld(store: ReviewStore) -> None:
    item = _register_builtin(store)
    store.set_status(item.item_id, ReviewStatus.APPROVED)
    assert store.get_withheld_keys(ContentType.BUILTIN) == set()


def test_get_withheld_source_ids(store: ReviewStore) -> None:
    store.register(
        item_id="external:openstax-finance",
        content_type=ContentType.EXTERNAL,
        domain="finance",
        filename="openstax-finance",
    )
    store.set_status("external:openstax-finance", ReviewStatus.REJECTED)
    assert "openstax-finance" in store.get_withheld_source_ids()


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


def test_set_priority(store: ReviewStore) -> None:
    item = _register_builtin(store)
    updated = store.set_priority(item.item_id, Priority.HIGH)
    assert updated.priority == Priority.HIGH


def test_get_priority_map_only_approved(store: ReviewStore) -> None:
    item_a = _register_builtin(store, filename="a.md")
    _register_builtin(store, filename="b.md")
    store.set_status(item_a.item_id, ReviewStatus.APPROVED)
    store.set_priority(item_a.item_id, Priority.HIGH)
    # item_b stays pending — should not appear in priority map

    pmap = store.get_priority_map(ContentType.BUILTIN)
    assert pmap.get(("finance", "a.md")) == "high"
    assert ("finance", "b.md") not in pmap


# ---------------------------------------------------------------------------
# Bulk approve
# ---------------------------------------------------------------------------


def test_bulk_approve_all_pending(store: ReviewStore) -> None:
    for fn in ["a.md", "b.md", "c.md"]:
        _register_builtin(store, filename=fn)
    ids = store.bulk_approve()
    assert len(ids) == 3
    for fn in ["a.md", "b.md", "c.md"]:
        item = store.get_item(f"builtin:finance:{fn}")
        assert item is not None
        assert item.status == ReviewStatus.APPROVED


def test_bulk_approve_domain_filter(store: ReviewStore) -> None:
    _register_builtin(store, domain="finance", filename="a.md")
    store.register(
        item_id="builtin:hr:b.md",
        content_type=ContentType.BUILTIN,
        domain="hr",
        filename="b.md",
    )
    ids = store.bulk_approve(domain="finance")
    assert ids == ["builtin:finance:a.md"]
    assert store.get_item("builtin:finance:a.md") is not None
    assert store.get_item("builtin:finance:a.md").status == ReviewStatus.APPROVED  # type: ignore[union-attr]
    assert store.get_item("builtin:hr:b.md") is not None
    assert store.get_item("builtin:hr:b.md").status == ReviewStatus.PENDING  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------


def test_add_and_list_annotations(store: ReviewStore) -> None:
    item = _register_builtin(store)
    ann = store.add_annotation(item.item_id, "finance", "Q3 burn rate is outdated")
    assert isinstance(ann, Annotation)
    assert ann.is_active is True

    listed = store.list_annotations(item_id=item.item_id)
    assert len(listed) == 1
    assert listed[0].correction == "Q3 burn rate is outdated"


def test_toggle_annotation(store: ReviewStore) -> None:
    item = _register_builtin(store)
    ann = store.add_annotation(item.item_id, "finance", "correction")
    store.toggle_annotation(ann.annotation_id, False)
    active = store.list_annotations(item_id=item.item_id, active_only=True)
    assert len(active) == 0
    all_anns = store.list_annotations(item_id=item.item_id, active_only=False)
    assert len(all_anns) == 1
    assert all_anns[0].is_active is False


def test_list_annotations_by_domain(store: ReviewStore) -> None:
    finance_item = _register_builtin(store, domain="finance", filename="a.md")
    hr_item = store.register(
        item_id="builtin:hr:b.md",
        content_type=ContentType.BUILTIN,
        domain="hr",
        filename="b.md",
    )
    store.add_annotation(finance_item.item_id, "finance", "finance note")
    store.add_annotation(hr_item.item_id, "hr", "hr note")

    finance_anns = store.list_annotations(domains=["finance"])
    assert len(finance_anns) == 1
    assert finance_anns[0].correction == "finance note"


def test_delete_annotation(store: ReviewStore) -> None:
    item = _register_builtin(store)
    ann = store.add_annotation(item.item_id, "finance", "to be deleted")
    store.delete_annotation(ann.annotation_id)
    assert store.list_annotations(item_id=item.item_id) == []


def test_delete_item_cascades_annotations(store: ReviewStore) -> None:
    item = _register_builtin(store)
    store.add_annotation(item.item_id, "finance", "note")
    store.delete_item(item.item_id)
    assert store.get_item(item.item_id) is None
    # Annotations should be cascade-deleted
    assert store.list_annotations(item_id=item.item_id) == []


# ---------------------------------------------------------------------------
# count_by_status
# ---------------------------------------------------------------------------


def test_count_by_status(store: ReviewStore) -> None:
    _register_builtin(store, filename="a.md")
    item_b = _register_builtin(store, filename="b.md")
    store.set_status(item_b.item_id, ReviewStatus.APPROVED)

    counts = store.count_by_status()
    assert counts["pending"] == 1
    assert counts["approved"] == 1
    assert counts["total"] == 2


# ---------------------------------------------------------------------------
# Trusted defaults: shipped content does not create a review backlog
# ---------------------------------------------------------------------------


def test_sync_builtin_registers_trusted_defaults(tmp_path: Path) -> None:
    """A fresh instance must start with an empty review queue.

    Shipped knowledge registers `approved` with a NULL `reviewed_at` — usable
    immediately, but still distinguishable from an item an SME signed off on.
    """
    db = tmp_path / "fresh.db"
    ReviewStore.initialize_db(db)
    registered = ReviewStore.sync_builtin_registrations(db)
    store = ReviewStore(db_path=db)

    assert registered > 0
    counts = store.count_by_status()
    assert counts["pending"] == 0
    assert counts["approved"] == registered
    assert all(i.reviewed_at is None for i in store.list_items(limit=500))
    # Nothing is withheld, so the Executive can use all of it on day one.
    assert store.get_withheld_keys(ContentType.BUILTIN) == set()


def test_builtin_filenames_are_unique_across_domains() -> None:
    """The retrieval filter keys on bare filename, not item_id.

    Two domains shipping the same basename would make a withheld item in one
    domain silently suppress the other. Guard the invariant the filter relies on.
    """
    from openexecutive.knowledge.loader import BUILTIN_KNOWLEDGE_PATH

    names = [
        md.name
        for md in BUILTIN_KNOWLEDGE_PATH.rglob("*.md")
        if "skills" not in md.parts
    ]
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"duplicate built-in basenames: {sorted(duplicates)}"


def test_sync_builtin_does_not_overwrite_an_sme_decision(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    ReviewStore.initialize_db(db)
    ReviewStore.sync_builtin_registrations(db)
    store = ReviewStore(db_path=db)

    victim = store.list_items(limit=1)[0]
    store.set_status(victim.item_id, ReviewStatus.REJECTED, "not for us")

    ReviewStore.sync_builtin_registrations(db)  # a later boot

    after = store.get_item(victim.item_id)
    assert after is not None
    assert after.status == ReviewStatus.REJECTED


def test_sync_external_registers_trusted_defaults(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    ReviewStore.initialize_db(db)
    ReviewStore.sync_external_registrations(
        [{"id": "oer-finance-101", "domains": ["finance"]}], db
    )
    item = ReviewStore(db_path=db).get_item("external:oer-finance-101")
    assert item is not None
    assert item.status == ReviewStatus.APPROVED
    assert item.reviewed_at is None


# ---------------------------------------------------------------------------
# Backfill: existing installs lose their phantom backlog, keep real decisions
# ---------------------------------------------------------------------------


def _legacy_pending(store: ReviewStore, filename: str) -> str:
    """Register an item the way the pre-trusted-defaults code did."""
    item = _register_builtin(store, filename=filename)
    assert item.status == ReviewStatus.PENDING
    return item.item_id


def _legacy_db(tmp_path: Path) -> Path:
    """A database created before trusted defaults existed.

    Old schema (no `review_meta`, so no backfill marker) plus the `pending`
    rows the old `sync_builtin_registrations` would have written. Calling
    `initialize_db` on this is exactly what an upgrade does.
    """
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE review_items (
            item_id          TEXT PRIMARY KEY,
            content_type     TEXT NOT NULL,
            domain           TEXT NOT NULL,
            filename         TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'pending',
            priority         TEXT NOT NULL DEFAULT 'normal',
            reviewer_notes   TEXT DEFAULT '',
            reviewed_at      TEXT,
            registered_at    TEXT NOT NULL,
            last_modified_at TEXT NOT NULL
        );
        CREATE TABLE review_annotations (
            annotation_id TEXT PRIMARY KEY,
            item_id       TEXT NOT NULL
                REFERENCES review_items(item_id) ON DELETE CASCADE,
            domain        TEXT NOT NULL,
            correction    TEXT NOT NULL,
            is_active     INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return db


def _shipped_pairs(n: int) -> list[tuple[str, str]]:
    """The first n real (domain, filename) pairs the builtin syncs register."""
    from openexecutive.knowledge.loader import BUILTIN_KNOWLEDGE_PATH

    return [
        (md.parent.name, md.name)
        for md in sorted(BUILTIN_KNOWLEDGE_PATH.rglob("*.md"))
        if "skills" not in md.parts
    ][:n]


def _seed_legacy_pending(db: Path, filename: str, domain: str = "finance") -> str:
    item_id = f"builtin:{domain}:{filename}"
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO review_items (item_id, content_type, domain, filename, "
        "registered_at, last_modified_at) VALUES (?, 'builtin', ?, ?, ?, ?)",
        (item_id, domain, filename, "2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    return item_id


def _register_default(store: ReviewStore, filename: str, domain: str = "finance") -> str:
    """Register an item in its shipped state: approved, trusted, never reviewed."""
    item_id = f"builtin:{domain}:{filename}"
    store.register(
        item_id=item_id,
        content_type=ContentType.BUILTIN,
        domain=domain,
        filename=filename,
    )
    with sqlite3.connect(store._db_path) as conn:
        conn.execute(
            "UPDATE review_items SET status = 'approved', trusted_default = 1 "
            "WHERE item_id = ?",
            (item_id,),
        )
    return item_id


def test_backfill_promotes_only_untouched_pending_rows(tmp_path: Path) -> None:
    db = _legacy_db(tmp_path)
    pairs = _shipped_pairs(3)
    untouched = _seed_legacy_pending(db, pairs[0][1], domain=pairs[0][0])
    decided = _seed_legacy_pending(db, pairs[1][1], domain=pairs[1][0])
    upload = _seed_legacy_pending(db, "my_own_upload.md", domain="finance")

    ReviewStore.initialize_db(db)  # adds the provenance column
    store = ReviewStore(db_path=db)
    store.set_status(decided, ReviewStatus.REJECTED, "off base")

    ReviewStore.sync_builtin_registrations(db)  # flags shipped provenance
    ReviewStore.backfill_trusted_defaults(db)  # the upgrade

    assert store.get_item(untouched).status == ReviewStatus.APPROVED  # type: ignore[union-attr]
    assert store.get_item(untouched).reviewed_at is None  # type: ignore[union-attr]
    # An explicit decision survives...
    assert store.get_item(decided).status == ReviewStatus.REJECTED  # type: ignore[union-attr]
    # ...and a user's own upload is never approved on their behalf.
    assert store.get_item(upload).status == ReviewStatus.PENDING  # type: ignore[union-attr]


def test_backfill_runs_once_and_does_not_undo_curation(tmp_path: Path) -> None:
    """Re-running init must not drag a deliberately curated domain back."""
    db = _legacy_db(tmp_path)
    domain, filename = _shipped_pairs(1)[0]
    item = _seed_legacy_pending(db, filename, domain=domain)

    ReviewStore.initialize_db(db)
    ReviewStore.sync_builtin_registrations(db)
    ReviewStore.backfill_trusted_defaults(db)
    store = ReviewStore(db_path=db)
    assert store.get_item(item).status == ReviewStatus.APPROVED  # type: ignore[union-attr]

    assert store.queue_for_curation(domain) >= 1
    ReviewStore.backfill_trusted_defaults(db)  # another boot

    assert store.get_item(item).status == ReviewStatus.PENDING  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# The pending gate
# ---------------------------------------------------------------------------


def test_withheld_covers_pending_and_rejected_but_not_needs_revision(
    store: ReviewStore,
) -> None:
    pending = _legacy_pending(store, "pending.md")
    rejected = _legacy_pending(store, "rejected.md")
    revising = _legacy_pending(store, "revising.md")
    approved = _legacy_pending(store, "approved.md")

    store.set_status(rejected, ReviewStatus.REJECTED)
    store.set_status(approved, ReviewStatus.APPROVED)
    store.set_status(revising, ReviewStatus.APPROVED)
    store.touch_modified(revising)  # an edit → needs_revision

    withheld = store.get_withheld_keys(ContentType.BUILTIN)

    assert ("finance", "pending.md") in withheld
    assert ("finance", "rejected.md") in withheld
    assert ("finance", "approved.md") not in withheld
    # An edited-but-previously-vetted file stays available: withholding it
    # would make editing a file in the knowledge UI silently delete it.
    assert store.get_item(revising).status == ReviewStatus.NEEDS_REVISION  # type: ignore[union-attr]
    assert ("finance", "revising.md") not in withheld
    assert pending in {i.item_id for i in store.list_items(status=ReviewStatus.PENDING)}


def test_withheld_source_ids_scope_to_external(store: ReviewStore) -> None:
    store.register(
        item_id="external:oer-1",
        content_type=ContentType.EXTERNAL,
        domain="finance",
        filename="oer-1",
    )
    _legacy_pending(store, "builtin-only.md")

    assert store.get_withheld_source_ids() == {"oer-1"}
    assert "builtin-only.md" not in store.get_withheld_source_ids()


# ---------------------------------------------------------------------------
# Opt-in curation
# ---------------------------------------------------------------------------


def test_curation_moves_only_untouched_defaults(store: ReviewStore) -> None:
    default_a = _register_default(store, "a.md")
    default_b = _register_default(store, "b.md")
    signed_off = _register_default(store, "signed.md")

    store.set_status(signed_off, ReviewStatus.APPROVED, "checked by our CFO")

    assert store.queue_for_curation("finance") == 2
    assert store.get_item(default_a).status == ReviewStatus.PENDING  # type: ignore[union-attr]
    assert store.get_item(default_b).status == ReviewStatus.PENDING  # type: ignore[union-attr]
    # An explicit approval is not curation fodder — it keeps its timestamp.
    assert store.get_item(signed_off).status == ReviewStatus.APPROVED  # type: ignore[union-attr]
    assert store.get_item(signed_off).reviewed_at is not None  # type: ignore[union-attr]

    assert store.stop_curation("finance") == 2
    assert store.get_item(default_a).status == ReviewStatus.APPROVED  # type: ignore[union-attr]
    assert store.get_item(default_a).reviewed_at is None  # type: ignore[union-attr]


def test_curation_is_domain_scoped(store: ReviewStore) -> None:
    _register_default(store, "fin.md")
    _register_default(store, "hr.md", domain="hr")

    assert store.queue_for_curation("finance") == 1
    assert store.get_item("builtin:hr:hr.md").status == ReviewStatus.APPROVED  # type: ignore[union-attr]


def test_count_trusted_defaults_by_domain(store: ReviewStore) -> None:
    _register_default(store, "a.md")
    _register_default(store, "h.md", domain="hr")
    decided = _register_default(store, "decided.md")
    store.set_status(decided, ReviewStatus.APPROVED, "vetted")

    assert store.count_trusted_defaults_by_domain() == {"finance": 1, "hr": 1}


# ---------------------------------------------------------------------------
# Bulk approve: explicit id selector
# ---------------------------------------------------------------------------


def test_bulk_approve_by_item_ids(store: ReviewStore) -> None:
    a = _legacy_pending(store, "a.md")
    _legacy_pending(store, "b.md")

    ids = store.bulk_approve(item_ids=[a])

    assert ids == [a]
    assert store.get_item("builtin:finance:b.md").status == ReviewStatus.PENDING  # type: ignore[union-attr]


def test_bulk_approve_empty_id_list_is_a_noop(store: ReviewStore) -> None:
    _legacy_pending(store, "a.md")
    assert store.bulk_approve(item_ids=[]) == []
    assert store.count_by_status()["pending"] == 1


def test_bulk_approve_ignores_already_decided_items(store: ReviewStore) -> None:
    rejected = _legacy_pending(store, "rejected.md")
    store.set_status(rejected, ReviewStatus.REJECTED, "no")

    assert store.bulk_approve(item_ids=[rejected]) == []
    assert store.get_item(rejected).status == ReviewStatus.REJECTED  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Provenance: shipped content vs the user's own uploads
# ---------------------------------------------------------------------------


def test_curation_never_touches_a_user_upload(store: ReviewStore) -> None:
    """A user's own document is also (pending, reviewed_at NULL).

    Inferring "this is a queued shipped default" from that shape made
    `stop_curation` approve someone's brand-new upload and count it as shipped
    content — a first review decision taken on their behalf by a button
    pressed for an unrelated reason.
    """
    shipped = _register_default(store, "shipped.md")
    upload = store.register(
        item_id="builtin:finance:my_upload.md",
        content_type=ContentType.BUILTIN,
        domain="finance",
        filename="my_upload.md",
    )
    assert upload.trusted_default is False
    assert store.get_item(shipped).trusted_default is True  # type: ignore[union-attr]

    # Simulate a restart: the sync must NOT re-flag the upload as shipped,
    # even though the upload physically lives in the built-in tree.
    ReviewStore.sync_builtin_registrations(store._db_path)
    assert store.get_item(upload.item_id).trusted_default is False  # type: ignore[union-attr]

    # Curating the domain queues the shipped docs; the upload is already
    # pending and is not something curation owns.
    queued = store.queue_for_curation("finance")
    assert queued >= 1
    assert store.get_item(shipped).status == ReviewStatus.PENDING  # type: ignore[union-attr]

    # Stopping restores exactly what it queued — never the upload, which keeps
    # waiting for a real first review.
    assert store.stop_curation("finance") == queued
    assert store.get_item(shipped).status == ReviewStatus.APPROVED  # type: ignore[union-attr]
    assert store.get_item(upload.item_id).status == ReviewStatus.PENDING  # type: ignore[union-attr]
    assert store.get_item(upload.item_id).trusted_default is False  # type: ignore[union-attr]


def test_backfill_never_approves_a_user_upload(tmp_path: Path) -> None:
    """The upgrade must not silently publish a doc a human left in the queue."""
    from openexecutive.knowledge.loader import BUILTIN_KNOWLEDGE_PATH

    db = _legacy_db(tmp_path)
    domain, _ = _shipped_pairs(1)[0]
    # The upload must physically exist in the built-in tree — that is exactly
    # where `POST /knowledge/builtin` puts it, and it is what made a directory
    # scan mistake it for shipped content.
    on_disk = BUILTIN_KNOWLEDGE_PATH / domain / "my_unvetted_memo.md"
    on_disk.write_text("# confidential draft", encoding="utf-8")
    try:
        upload = _seed_legacy_pending(db, on_disk.name, domain=domain)

        ReviewStore.initialize_db(db)
        ReviewStore.sync_builtin_registrations(db)  # must never flag the upload
        ReviewStore.backfill_trusted_defaults(db)
        store = ReviewStore(db_path=db)

        item = store.get_item(upload)
        assert item is not None
        assert item.trusted_default is False
        assert item.status == ReviewStatus.PENDING
    finally:
        on_disk.unlink()


def test_backfill_keeps_annotated_shipped_docs_retrievable(tmp_path: Path) -> None:
    """No shipped doc may lose retrievability on upgrade.

    Annotations are injected into specialist context by DOMAIN regardless of
    item status, so holding an annotated doc back would feed the Executive a
    correction to a document it can no longer see.
    """
    db = _legacy_db(tmp_path)
    ReviewStore.initialize_db(db)
    ReviewStore.sync_builtin_registrations(db)
    store = ReviewStore(db_path=db)

    # Put every shipped row back to the pre-change 'pending' state, then have
    # an SME annotate one and leave a note on another.
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE review_items SET status = 'pending'")
    annotated, noted = (i.item_id for i in store.list_items(limit=2))
    store.add_annotation(annotated, "finance", "we use a 12% WACC, not 8%")
    store.update_notes(noted, "double-check this one")

    ReviewStore.backfill_trusted_defaults(db)

    assert store.count_by_status()["pending"] == 0
    assert store.get_withheld_keys(ContentType.BUILTIN) == set()
    # The correction survives alongside the doc it corrects.
    assert len(store.list_annotations(item_id=annotated)) == 1


def test_backfill_is_concurrency_safe(tmp_path: Path) -> None:
    """Two processes booting at once must not crash on the marker row."""
    import threading

    db = _legacy_db(tmp_path)
    for domain, filename in _shipped_pairs(20):
        _seed_legacy_pending(db, filename, domain=domain)
    ReviewStore.initialize_db(db)
    ReviewStore.sync_builtin_registrations(db)

    errors: list[Exception] = []

    def _boot() -> None:
        try:
            ReviewStore.backfill_trusted_defaults(db)
        except Exception as exc:  # pragma: no cover - only on regression
            errors.append(exc)

    threads = [threading.Thread(target=_boot) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


def test_bulk_approve_rejects_an_oversized_id_list(store: ReviewStore) -> None:
    """Silently dropping the tail would under-approve without telling anyone."""
    from openexecutive.knowledge.review_store import BULK_MAX_IDS

    with pytest.raises(ValueError, match="BULK_MAX_IDS"):
        store.bulk_approve(item_ids=[f"builtin:x:{i}.md" for i in range(BULK_MAX_IDS + 1)])


def test_shipped_manifest_matches_git(tmp_path: Path) -> None:
    """The manifest must list exactly the built-in docs the repo ships.

    It cannot be derived from the filesystem at runtime — a user's upload lands
    in the same tree — so it is committed, and this guards it against drift.
    Adding a knowledge doc without regenerating the manifest fails here.
    """
    import subprocess

    from openexecutive.knowledge.shipped_manifest import SHIPPED_BUILTIN_FILES

    prefix = "packages/core/openexecutive/knowledge/builtin/"
    tracked = subprocess.run(
        ["git", "ls-files", f"{prefix}**/*.md"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[4],
        check=True,
    ).stdout.split()
    expected = {
        p[len(prefix):] for p in tracked
        if p.startswith(prefix) and "/skills/" not in p
    }

    assert expected, "git ls-files returned nothing — check the path prefix"
    missing = expected - SHIPPED_BUILTIN_FILES
    extra = SHIPPED_BUILTIN_FILES - expected
    assert not missing, f"regenerate shipped_manifest.py — missing: {sorted(missing)}"
    assert not extra, f"regenerate shipped_manifest.py — no longer shipped: {sorted(extra)}"


def test_sync_ignores_an_unmanifested_file_in_the_builtin_tree() -> None:
    """A user upload sits in the shipped tree but must never be registered by the sync."""
    from openexecutive.knowledge.loader import BUILTIN_KNOWLEDGE_PATH
    from openexecutive.knowledge.shipped_manifest import SHIPPED_BUILTIN_FILES

    intruder = BUILTIN_KNOWLEDGE_PATH / "finance" / "user_upload_probe.md"
    intruder.write_text("# not shipped", encoding="utf-8")
    try:
        assert "finance/user_upload_probe.md" not in SHIPPED_BUILTIN_FILES
    finally:
        intruder.unlink()


# ---------------------------------------------------------------------------
# Namespace separation: a user upload must never land on a shipped row
# ---------------------------------------------------------------------------


def _shipped_failure_pair() -> tuple[str, str]:
    """A real (domain, filename) from the manifest's failures/ entries."""
    from pathlib import PurePosixPath

    from openexecutive.knowledge.shipped_manifest import SHIPPED_BUILTIN_FILES

    for rel in sorted(SHIPPED_BUILTIN_FILES):
        p = PurePosixPath(rel)
        if p.parts[0] == "failures":
            return p.parent.name, p.name
    raise AssertionError("manifest has no failures/ entries")


def test_failure_docs_do_not_occupy_the_upload_namespace(tmp_path: Path) -> None:
    """The bug: a shipped failure doc used to own `builtin:<domain>:<file>`.

    That is the id `POST /knowledge/builtin` writes to, and since no file sits
    at `knowledge/builtin/<domain>/<file>` for a failure doc, that route's
    `path.exists()` 409 could not fire — the upload INSERT-OR-IGNOREd onto the
    shipped row and inherited `approved` + `trusted_default = 1`.
    """
    from openexecutive.knowledge.review_store import build_item_id

    db = tmp_path / "r.db"
    ReviewStore.initialize_db(db)
    ReviewStore.sync_builtin_registrations(db)
    store = ReviewStore(db_path=db)

    domain, filename = _shipped_failure_pair()
    upload_id = build_item_id(ContentType.BUILTIN, domain, filename)
    shipped_id = build_item_id(ContentType.FAILURE, domain, filename)

    assert upload_id != shipped_id
    # The shipped doc is registered, trusted, under the FAILURE namespace.
    shipped = store.get_item(shipped_id)
    assert shipped is not None and shipped.trusted_default is True
    # ...and the upload namespace is free.
    assert store.get_item(upload_id) is None

    # An upload of that exact name gets its own untrusted, withheld row.
    store.register(
        item_id=upload_id,
        content_type=ContentType.BUILTIN,
        domain=domain,
        filename=filename,
    )
    upload = store.get_item(upload_id)
    assert upload is not None
    assert upload.trusted_default is False
    assert upload.status == ReviewStatus.PENDING
    # The shipped doc is untouched by the upload.
    assert store.get_item(shipped_id).trusted_default is True  # type: ignore[union-attr]
    assert store.get_item(shipped_id).status == ReviewStatus.APPROVED  # type: ignore[union-attr]


def test_failure_namespace_migration_carries_the_sme_decision(tmp_path: Path) -> None:
    """An existing DB has failure rows under the old `builtin:` id.

    Re-keying them must not lose the reviewer's decision, notes, or
    annotations — those are the only record of a human's judgement.
    """
    from openexecutive.knowledge.review_store import build_item_id

    db = _legacy_db(tmp_path)
    domain, filename = _shipped_failure_pair()
    old_id = _seed_legacy_pending(db, filename, domain=domain)

    ReviewStore.initialize_db(db)
    store = ReviewStore(db_path=db)
    store.set_status(old_id, ReviewStatus.REJECTED, "legally unsound")
    store.add_annotation(old_id, domain, "our counsel disagrees")

    ReviewStore.sync_builtin_registrations(db)  # runs the migration

    new_id = build_item_id(ContentType.FAILURE, domain, filename)
    assert store.get_item(old_id) is None, "old id must not linger"
    migrated = store.get_item(new_id)
    assert migrated is not None
    assert migrated.status == ReviewStatus.REJECTED
    assert migrated.reviewer_notes == "legally unsound"
    anns = store.list_annotations(item_id=new_id)
    assert [a.correction for a in anns] == ["our counsel disagrees"]

    # Idempotent.
    ReviewStore.sync_builtin_registrations(db)
    assert store.get_item(new_id).status == ReviewStatus.REJECTED  # type: ignore[union-attr]


def test_withheld_key_is_domain_qualified(store: ReviewStore) -> None:
    """A bare filename was ambiguous AND user-choosable.

    `POST /knowledge/builtin` lets a caller pick any (domain, filename), so
    uploading a file named after a shipped doc in another domain silently
    withheld that shipped doc from every specialist.

    The withheld row deliberately sits in a domain that appears nowhere else
    in this test, and the assertion is exact set equality — a key that dropped
    or hardcoded the domain would still satisfy a mere `in` check.
    """
    store.register(
        item_id="builtin:hr:product_strategy.md",
        content_type=ContentType.BUILTIN,
        domain="hr",
        filename="product_strategy.md",
    )
    shipped = store.register(
        item_id="builtin:product:product_strategy.md",
        content_type=ContentType.BUILTIN,
        domain="product",
        filename="product_strategy.md",
    )
    store.set_status(shipped.item_id, ReviewStatus.APPROVED)

    withheld = store.get_withheld_keys(ContentType.BUILTIN)

    assert withheld == {("hr", "product_strategy.md")}
    # The same basename in another domain is NOT suppressed.
    assert ("product", "product_strategy.md") not in withheld


def test_priority_map_key_is_domain_qualified(store: ReviewStore) -> None:
    """Same ambiguity, same fix — a bare filename would collide across domains."""
    a = store.register(
        item_id="builtin:hr:handbook.md",
        content_type=ContentType.BUILTIN,
        domain="hr",
        filename="handbook.md",
    )
    b = store.register(
        item_id="builtin:legal:handbook.md",
        content_type=ContentType.BUILTIN,
        domain="legal",
        filename="handbook.md",
    )
    store.set_status(a.item_id, ReviewStatus.APPROVED)
    store.set_status(b.item_id, ReviewStatus.APPROVED)
    store.set_priority(a.item_id, Priority.HIGH)

    pmap = store.get_priority_map(ContentType.BUILTIN)

    assert pmap[("hr", "handbook.md")] == "high"
    assert pmap[("legal", "handbook.md")] == "normal"


def test_legacy_upgrade_does_not_requeue_failure_docs(tmp_path: Path) -> None:
    """The migration and the backfill have to compose correctly.

    On a pre-#119 database every shipped row is `pending` under a `builtin:`
    id. The migration copies that `pending` onto the new `failure:` row — so
    on its own it would hand back 17 of the 81 backlog items this PR exists to
    remove. `backfill_trusted_defaults`, which runs afterwards, promotes them
    because they are `trusted_default = 1` with a NULL `reviewed_at`. Pinning
    the pair: neither step is correct alone.
    """
    import sqlite3
    from pathlib import PurePosixPath

    from openexecutive.knowledge.review_store import build_item_id
    from openexecutive.knowledge.shipped_manifest import SHIPPED_BUILTIN_FILES

    db = tmp_path / "legacy.db"
    ReviewStore.initialize_db(db)
    ReviewStore.sync_builtin_registrations(db)

    # Rewind to the pre-#119 world: no markers, everything pending, failure
    # docs sitting in the `builtin:` namespace.
    fails = [
        PurePosixPath(r)
        for r in sorted(SHIPPED_BUILTIN_FILES)
        if PurePosixPath(r).parts[0] == "failures"
    ]
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE review_items SET status='pending', reviewed_at=NULL")
        conn.execute("DELETE FROM review_meta")
        conn.execute("DELETE FROM review_items WHERE content_type='failure'")
        for p in fails:
            conn.execute(
                "INSERT OR IGNORE INTO review_items (item_id, content_type, domain, "
                "filename, status, trusted_default, registered_at, last_modified_at) "
                "VALUES (?, 'builtin', ?, ?, 'pending', 1, '2025-01-01', '2025-01-01')",
                (f"builtin:{p.parent.name}:{p.name}", p.parent.name, p.name),
            )

    store = ReviewStore(db_path=db)
    assert store.count_by_status()["pending"] == store.count_by_status()["total"]

    # The upgrade, in the order api/main.py runs it.
    ReviewStore.initialize_db(db)
    ReviewStore.sync_builtin_registrations(db)
    ReviewStore.backfill_trusted_defaults(db)

    counts = store.count_by_status()
    assert counts["pending"] == 0, "the backlog must not come back"
    assert counts["approved"] == counts["total"]
    # Old ids are gone; the failure gate has nothing withheld.
    d0, f0 = fails[0].parent.name, fails[0].name
    assert store.get_item(f"builtin:{d0}:{f0}") is None
    assert store.get_item(build_item_id(ContentType.FAILURE, d0, f0)) is not None
    assert store.get_withheld_keys(ContentType.FAILURE) == set()


def test_migration_preserves_a_deliberate_curation(tmp_path: Path) -> None:
    """The other merge direction, which the backfill cannot rescue.

    On a database already upgraded by an earlier commit the backfill marker is
    set, so it is a no-op. If the operator had curated a domain containing
    failure docs, those rows are `pending` ON PURPOSE. The migration copies
    that across, and nothing promotes it back — which is exactly right, and is
    why the merge direction has to be correct on its own rather than relying
    on the backfill to clean up after it.
    """
    import sqlite3
    from pathlib import PurePosixPath

    from openexecutive.knowledge.review_store import build_item_id
    from openexecutive.knowledge.shipped_manifest import SHIPPED_BUILTIN_FILES

    db = tmp_path / "curated.db"
    ReviewStore.initialize_db(db)
    ReviewStore.sync_builtin_registrations(db)
    ReviewStore.backfill_trusted_defaults(db)

    p = next(
        PurePosixPath(r)
        for r in sorted(SHIPPED_BUILTIN_FILES)
        if PurePosixPath(r).parts[0] == "failures"
    )
    domain = p.parent.name
    # Rewind just the failure namespace, leaving a curated `builtin:` row.
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM review_items WHERE content_type='failure'")
        conn.execute("DELETE FROM review_meta WHERE key='failure_namespace_v1'")
        conn.execute(
            "INSERT INTO review_items (item_id, content_type, domain, filename, "
            "status, trusted_default, registered_at, last_modified_at) "
            "VALUES (?, 'builtin', ?, ?, 'pending', 1, '2025-01-01', '2025-01-01')",
            (f"builtin:{domain}:{p.name}", domain, p.name),
        )

    ReviewStore.sync_builtin_registrations(db)
    ReviewStore.backfill_trusted_defaults(db)  # marker already set: no-op

    store = ReviewStore(db_path=db)
    migrated = store.get_item(build_item_id(ContentType.FAILURE, domain, p.name))
    assert migrated is not None
    assert migrated.status == ReviewStatus.PENDING, "curation intent must survive"
    # And the operator can still back out of it.
    assert store.stop_curation(domain) >= 1
    assert store.get_item(migrated.item_id).status == ReviewStatus.APPROVED  # type: ignore[union-attr]


def test_migration_does_not_hijack_a_merged_user_upload(tmp_path: Path) -> None:
    """The population this migration exists for is where the old bug FIRED.

    There, one `builtin:<d>:<f>` row governed two files: the shipped failure
    doc and a user upload that INSERT-OR-IGNOREd onto it. Transplanting that
    row's decision onto the case study both mis-applies the SME's judgement
    and deletes the only row governing the upload — silently un-suppressing
    content someone had rejected, with no row left to re-reject it from.
    """
    import sqlite3
    from pathlib import PurePosixPath

    from openexecutive.knowledge.loader import BUILTIN_KNOWLEDGE_PATH
    from openexecutive.knowledge.review_store import build_item_id
    from openexecutive.knowledge.shipped_manifest import SHIPPED_BUILTIN_FILES

    p = next(
        PurePosixPath(r)
        for r in sorted(SHIPPED_BUILTIN_FILES)
        if PurePosixPath(r).parts[0] == "failures"
    )
    domain, filename = p.parent.name, p.name

    db = tmp_path / "merged.db"
    ReviewStore.initialize_db(db)
    ReviewStore.sync_builtin_registrations(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM review_items WHERE content_type='failure'")
        conn.execute("DELETE FROM review_meta")
        conn.execute(
            "INSERT INTO review_items (item_id, content_type, domain, filename, "
            "status, trusted_default, reviewed_at, reviewer_notes, registered_at, "
            "last_modified_at) VALUES (?, 'builtin', ?, ?, 'rejected', 1, "
            "'2025-06-01', 'confidential draft', '2025-01-01', '2025-01-01')",
            (f"builtin:{domain}:{filename}", domain, filename),
        )

    # The upload really is on disk — that is what makes the row ambiguous.
    planted = BUILTIN_KNOWLEDGE_PATH / domain / filename
    planted.write_text("# planted", encoding="utf-8")
    try:
        ReviewStore.sync_builtin_registrations(db)
        ReviewStore.backfill_trusted_defaults(db)
        store = ReviewStore(db_path=db)

        upload = store.get_item(f"builtin:{domain}:{filename}")
        assert upload is not None, "the row governing the upload must survive"
        assert upload.status == ReviewStatus.REJECTED, "the SME decision stands"
        assert upload.trusted_default is False, "collision-conferred trust is stripped"
        assert (domain, filename) in store.get_withheld_keys(ContentType.BUILTIN)

        shipped = store.get_item(build_item_id(ContentType.FAILURE, domain, filename))
        assert shipped is not None
        assert shipped.status == ReviewStatus.APPROVED, "no transplanted rejection"
        assert shipped.trusted_default is True
    finally:
        planted.unlink()


def test_builtin_chunk_domain_matches_its_review_row(tmp_path: Path) -> None:
    """The gate key is `(domain, filename)` — both sides must derive it the same.

    Chunk metadata used `infer_domain_from_path` over the ABSOLUTE path, so an
    install under e.g. `/srv/product/` tagged every chunk `product` while
    review rows used the directory name. No key would ever match and the gate
    would fail wide open.
    """
    from openexecutive.knowledge.loader import (
        BUILTIN_KNOWLEDGE_PATH,
        infer_domain_from_path,
    )
    from openexecutive.knowledge.shipped_manifest import SHIPPED_BUILTIN_FILES

    for rel in sorted(SHIPPED_BUILTIN_FILES):
        path = BUILTIN_KNOWLEDGE_PATH / rel
        chunk_domain = infer_domain_from_path(path, root=BUILTIN_KNOWLEDGE_PATH)
        assert chunk_domain == path.parent.name, f"{rel}: {chunk_domain}"

    # And it is immune to a domain word in the install prefix.
    root = Path("/srv/product/app/builtin")
    assert infer_domain_from_path(root / "strategy" / "x.md", root=root) == "strategy"
