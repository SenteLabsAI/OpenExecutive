"""Doc/code consistency guards.

These tests fail when the curated architecture facts drift from what the code
actually does — the failure mode the ``/architecture`` page is most prone to
(see ``CLAUDE.md`` -> "Architecture Docs"). They are deliberately coupled to
BOTH the YAML and the code, so changing one without the other breaks CI.

Current coverage: the ``wait_for_human`` resume invariant (the canonical drift
example — the docs and the code have now disagreed in BOTH directions, first
claiming paused workflows auto-continued when they did not, and later still
calling resume deferred after it shipped). Add further invariants here as they
are identified.
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


def _code_resumes() -> bool:
    """Whether the code can actually continue a run past its gate.

    Structural, not textual: the executor and the engine entry point must both
    exist. A comment can be edited to say anything; these cannot.
    """
    from openexecutive.workflows.dynamic import DynamicWorkflow

    return (
        hasattr(resumer, "_execute_resume")
        and hasattr(resumer, "_process_resumable")
        and hasattr(DynamicWorkflow, "resume")
    )


def test_resumer_implements_generator_resume() -> None:
    """Pins the shipped reality: a resolved run with a resume payload is
    claimed and executed. Previously this guard pinned the OPPOSITE — that
    resume was deferred to "Phase 7" — and it fired when resume landed, which
    is exactly what it was for. It now guards the other direction: if the
    executor is removed or renamed, update the facts and this test together."""
    assert _code_resumes(), (
        "resumer no longer exposes the resume executor. If resume was removed, "
        "update architecture-facts.yaml (workflows.human_in_the_loop) and this "
        "test together."
    )
    # The capture path resume builds on must still exist.
    assert hasattr(resumer, "apply_resolution")
    # The deferral marker must be gone from the source, or the module is
    # describing behaviour it no longer has.
    assert "Phase 7" not in _resumer_source(), (
        "resumer.py still marks full generator resume as deferred to Phase 7, "
        "but the executor exists. Remove the stale note."
    )


def test_hitl_doc_does_not_understate_resume() -> None:
    """The facts must not still describe resume as unshipped now that it is.

    The mirror of the original guard: that one caught the docs overclaiming,
    this one catches them underclaiming. Both are the same failure — the page
    saying something the code does not do."""
    hitl = _load_facts()["workflows"]["human_in_the_loop"]

    assert "NOT YET SHIPPED" not in hitl, (
        "architecture-facts.yaml still lists a NOT YET SHIPPED resume gap, but "
        "resumer executes resolved runs."
    )
    assert "Phase 7" not in hitl
    # And it must positively describe the mechanism, so the doc is useful and
    # not merely silent about it.
    assert "resume_state_json" in hitl, (
        "workflows.human_in_the_loop should name the column the resume payload "
        "is stored in, so the page explains HOW a run continues."
    )


def test_hitl_doc_and_code_agree_on_resume() -> None:
    """Couple the two directly: the doc admits a resume gap if and only if the
    code has one. The one assertion that ties code reality to the page."""
    hitl = _load_facts()["workflows"]["human_in_the_loop"]
    doc_admits_gap = ("Phase 7" in hitl) or ("NOT YET SHIPPED" in hitl)
    assert _code_resumes() != doc_admits_gap, (
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


def _delegation_facts() -> dict[str, Any]:
    facts: dict[str, Any] = _load_facts()["delegation"]
    return facts


def test_act_as_me_send_claim_and_code_agree() -> None:
    """Act as me drafts and never sends. The page says so; the Gmail client
    must have no send path. Whoever adds sending (a later phase) updates
    architecture-facts.yaml (delegation.gmail_client) and this test together."""
    import re

    from openexecutive.delegation import gmail

    doc_says_no_send = "NO send method" in _delegation_facts()["gmail_client"]
    code_can_send = re.search(
        r"/(messages|drafts)/send\b", Path(gmail.__file__).read_text()
    ) is not None
    assert doc_says_no_send != code_can_send, (
        "Whether Act as me can send disagrees between delegation/gmail.py and "
        "architecture-facts.yaml (delegation.gmail_client). Update both."
    )


def test_act_as_me_tool_registry_claim_and_code_agree() -> None:
    """The facts say ghostwrite_email never joins _ALL_SKILL_TOOLS — the list
    reflection, research and workflow toolkits are built from. Couple that
    claim to the registry itself."""
    from openexecutive.orchestrator.delegation_tools import DELEGATION_TOOL_NAMES
    from openexecutive.orchestrator.executive import _ALL_SKILL_HANDLERS, _ALL_SKILL_TOOLS

    doc_says_separate = "NEVER _ALL_SKILL_TOOLS" in _delegation_facts()["tool"]
    code_is_separate = not (
        DELEGATION_TOOL_NAMES & {t["name"] for t in _ALL_SKILL_TOOLS}
        or DELEGATION_TOOL_NAMES & set(_ALL_SKILL_HANDLERS)
    )
    assert doc_says_separate and code_is_separate, (
        "ghostwrite_email must stay out of _ALL_SKILL_TOOLS / _ALL_SKILL_HANDLERS, "
        "as architecture-facts.yaml (delegation.tool) says."
    )
