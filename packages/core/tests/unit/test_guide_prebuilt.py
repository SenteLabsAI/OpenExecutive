"""Unit tests for the static user-guide content loader.

These double as a content lint: every section in the registry must ship a
well-formed pre-authored file, and the guide is intentionally diagram-free.
"""
from __future__ import annotations

from openexecutive.guide import prebuilt
from openexecutive.guide.sections import GUIDE_SECTIONS
from openexecutive.utils.prebuilt_store import REQUIRED_KEYS

_REQUIRED_KEYS = REQUIRED_KEYS


def test_every_section_has_prebuilt_content() -> None:
    for spec in GUIDE_SECTIONS:
        data = prebuilt.get_prebuilt(spec.id)
        assert data is not None, f"missing prebuilt content for {spec.id}"
        assert set(data) >= _REQUIRED_KEYS, f"bad keys for {spec.id}"
        assert data["section_id"] == spec.id
        assert data["markdown"].strip(), f"empty markdown for {spec.id}"


def test_list_prebuilt_covers_registry_exactly() -> None:
    listed = set(prebuilt.list_prebuilt())
    registry = {s.id for s in GUIDE_SECTIONS}
    assert listed == registry


def test_markdown_has_no_top_level_heading() -> None:
    # The UI renders the section title; the body must not repeat it.
    for spec in GUIDE_SECTIONS:
        data = prebuilt.get_prebuilt(spec.id)
        assert data is not None
        assert not data["markdown"].lstrip().startswith(
            "#"
        ), f"{spec.id}: markdown must not start with a heading (UI renders the title)"


def test_guide_is_diagram_free() -> None:
    # The user guide is plain-language prose by design — no Mermaid.
    for spec in GUIDE_SECTIONS:
        data = prebuilt.get_prebuilt(spec.id)
        assert data is not None
        assert data["mermaid"] is None, f"{spec.id} should have no diagram"


def test_every_section_has_how_to_use() -> None:
    # Each section explains how to use the feature, not just what it is.
    for spec in GUIDE_SECTIONS:
        data = prebuilt.get_prebuilt(spec.id)
        assert data is not None
        assert (
            "**How to use:**" in data["markdown"]
        ), f"{spec.id}: add a **How to use:** walkthrough"


def test_get_prebuilt_unknown_returns_none() -> None:
    assert prebuilt.get_prebuilt("does-not-exist") is None


def test_get_prebuilt_rejects_path_traversal() -> None:
    assert prebuilt.get_prebuilt("../cache") is None
    assert prebuilt.get_prebuilt("..") is None
    assert prebuilt.get_prebuilt("a/b") is None
