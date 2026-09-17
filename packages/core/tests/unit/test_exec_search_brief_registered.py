"""`exec_search_brief` survived the talent-vertical removal: it is an advisory
hiring brief from the CHRO, not part of the candidate pipeline."""
from __future__ import annotations

from openexecutive.workflows import WORKFLOW_REGISTRY
from openexecutive.workflows.exec_search_brief import ExecSearchBriefWorkflow


def test_exec_search_brief_is_registered_and_builds_inputs() -> None:
    wf = WORKFLOW_REGISTRY["exec_search_brief"]
    assert isinstance(wf, ExecSearchBriefWorkflow)
    fields = wf.input_model().model_fields
    assert "role_title" in fields
    assert not wf.meta().is_custom
