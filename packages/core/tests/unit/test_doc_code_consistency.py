"""Doc/code consistency guards.

These tests fail when the curated architecture facts drift from what the code
actually does — the failure mode the ``/architecture`` page is most prone to
(see ``CLAUDE.md`` -> "Architecture Docs"). They are deliberately coupled to
BOTH the YAML and the code, so changing one without the other breaks CI.

Current coverage: the ``wait_for_human`` resume invariant (the canonical drift
example — docs once claimed paused workflows auto-continue while the code
defers full generator resume to "Phase 7"). Add further invariants here as
they are identified.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from openexecutive.architecture import facts as facts_mod
from openexecutive.workflows import resumer


def _load_facts() -> dict[str, Any]:
    path = Path(facts_mod.__file__).parent / "architecture-facts.yaml"
    return yaml.safe_load(path.read_text())


def _resumer_source() -> str:
    return Path(resumer.__file__).read_text()


def test_resumer_still_defers_full_generator_resume() -> None:
    """Pins the code reality the docs describe: full generator resume is NOT
    implemented (deferred to "Phase 7"). If someone implements it and removes
    the marker, this fails — prompting them to update both the code note and
    the architecture facts (see the doc test below)."""
    src = _resumer_source()
    assert "Phase 7" in src, (
        "resumer.py no longer marks full generator resume as deferred. If "
        "resume was implemented, update architecture-facts.yaml "
        "(workflows.human_in_the_loop) and this test together."
    )
    # The capture path that IS shipped must still exist...
    assert hasattr(resumer, "apply_resolution")
    # ...and no public auto-continue entry point should exist yet.
    assert not hasattr(resumer, "resume_run")
    assert not hasattr(resumer, "continue_run")


def test_hitl_doc_does_not_overclaim_resume() -> None:
    """The facts must not claim paused workflows auto-continue while the code
    defers that — the exact drift this guard exists to catch."""
    hitl = _load_facts()["workflows"]["human_in_the_loop"]

    # The retired overclaim: resumer "continues the next step".
    assert "continues the next step" not in hitl, (
        "architecture-facts.yaml claims wait_for_human continues the next "
        "step, but resumer.py defers full generator resume to Phase 7."
    )
    # And it must positively signal the deferral so the doc stays honest.
    assert ("Phase 7" in hitl) or ("NOT YET SHIPPED" in hitl)


def test_hitl_doc_and_code_agree_on_resume() -> None:
    """Couple the two directly: if the code defers resume, the doc must say so
    (and vice versa). The one assertion that ties code reality to the page."""
    code_defers = "Phase 7" in _resumer_source()
    hitl = _load_facts()["workflows"]["human_in_the_loop"]
    doc_admits_gap = ("Phase 7" in hitl) or ("NOT YET SHIPPED" in hitl)
    assert code_defers == doc_admits_gap, (
        "Resume status disagrees between resumer.py and architecture-facts.yaml "
        "(workflows.human_in_the_loop). Update both."
    )


def _retriever_source() -> str:
    from openexecutive.knowledge import retriever

    return Path(retriever.__file__).read_text()


def test_retriever_never_queries_the_attachment_collection() -> None:
    """The isolation is 'this collection is not queried', which is only ever
    one helpful edit away from being untrue. Coupled to the constant rather
    than the literal so a rename cannot quietly defeat it."""
    from openexecutive.knowledge.store import ChromaDBStore

    src = _retriever_source()
    assert "ATTACHMENT_COLLECTION" not in src, (
        "retriever.py now references ATTACHMENT_COLLECTION. Attachments are "
        "sender-chosen, unreviewed text with no delete path; retrieving them "
        "is a trust-boundary change that needs its own decision and an update "
        "to architecture-facts.yaml (knowledge.collections)."
    )
    assert ChromaDBStore.ATTACHMENT_COLLECTION not in src


def test_attachment_facts_do_not_claim_domain_isolation() -> None:
    """The retired overclaim: that a non-specialist domain kept attachment
    chunks out of the Executive's context. An unfiltered retrieval builds no
    `where` clause, so it matched every domain."""
    attachments = _load_facts()["integrations"]["attachments"]

    assert "never reach the Executive" not in attachments
    assert "match no domain filter" not in attachments
    # And it must positively name the mechanism that does the work.
    from openexecutive.knowledge.store import ChromaDBStore

    assert ChromaDBStore.ATTACHMENT_COLLECTION in attachments


def test_attachment_isolation_doc_and_code_agree() -> None:
    """Couple the two: the code isolates by collection, so the doc must say
    collection — not domain."""
    from openexecutive.integrations import attachments as att_mod
    from openexecutive.knowledge.store import ChromaDBStore

    code_isolates_by_collection = (
        "ATTACHMENT_COLLECTION" in Path(att_mod.__file__).read_text()
    )
    doc_names_collection = (
        ChromaDBStore.ATTACHMENT_COLLECTION
        in _load_facts()["integrations"]["attachments"]
    )
    assert code_isolates_by_collection == doc_names_collection, (
        "Attachment isolation disagrees between integrations/attachments.py "
        "and architecture-facts.yaml (integrations.attachments). Update both."
    )
