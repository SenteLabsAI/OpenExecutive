"""EVAL_JUDGE_MODEL picks the model that scores eval answers.

Unset, the judge stays on Claude Opus. Set, every judge call goes to that
model through the provider registry, so a slug listed in LOCAL_MODELS is
judged by the local server (an eval run with no hosted API at all).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from openexecutive.evals import judges as judges_module


class _Msg:
    content = [type("T", (), {"type": "text", "text": '{"overall": 4, "notes": "ok"}'})()]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []

    class _Provider:
        def __init__(self, routed: str) -> None:
            self.routed = routed

        async def messages_create(self, **kwargs: Any) -> Any:
            seen.append((self.routed, kwargs))
            return _Msg()

    monkeypatch.setattr(judges_module, "get_provider", _Provider)
    monkeypatch.setattr(judges_module, "log_model_usage", lambda *a, **k: None)
    return seen


def test_unset_judges_with_opus(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, dict[str, Any]]]
) -> None:
    monkeypatch.delenv("EVAL_JUDGE_MODEL", raising=False)

    scores = asyncio.run(judges_module.judge_chat({"query": "q", "expected_topics": []}, "r"))

    assert scores["overall"] == 4
    assert calls == [("claude-opus-5-5", calls[0][1])]
    assert calls[0][1]["model"] == "claude-opus-5-5"


def test_set_routes_every_judge_to_that_model(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, dict[str, Any]]]
) -> None:
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "qwen3:4b")

    asyncio.run(judges_module.judge_chat({"query": "q", "expected_topics": []}, "r"))
    asyncio.run(
        judges_module.judge_workflow(
            {"workflow": "w", "description": "d", "expected_sections": []}, "artifact"
        )
    )

    assert [routed for routed, _ in calls] == ["qwen3:4b", "qwen3:4b"]
    assert all(kwargs["model"] == "qwen3:4b" for _, kwargs in calls)


def test_blank_value_reads_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "")

    assert judges_module.judge_model() == "claude-opus-5-5"
