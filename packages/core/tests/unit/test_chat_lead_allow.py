"""The "+ on its own next time" button on a Take the lead card tapped in a
chat app (api/routes/decisions.resolve_in_chat) makes the web card's checks
before anything is done: a training card with a grant, the owner's own tap,
and room on the allowed list."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from openexecutive.api.routes import decisions

OWNER = SimpleNamespace(id=1, email="olivia@co.example")
TEAMMATE = SimpleNamespace(id=2, email="sam@co.example")


def _card(payload: dict[str, Any], approver: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=7, decision_class="take_the_lead_action", approver_person_id=approver, status=decisions.STATUS_PROPOSED,
        proposed_payload_json=json.dumps(payload), channel="telegram",
    )


@pytest.fixture
def lead(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"card": None, "room": True, "ran": []}
    monkeypatch.setattr(decisions, "get_decision_instance", lambda i, **k: state["card"])
    monkeypatch.setattr(decisions, "shown_in_chat", lambda inst: True)
    monkeypatch.setattr("openexecutive.integrations.channel_cards.card_text", lambda i: "card")
    monkeypatch.setattr("openexecutive.people.store.find_principal_person", lambda: OWNER)
    monkeypatch.setattr(decisions, "_spec_for", lambda inst: SimpleNamespace(name="take_the_lead_action"))
    monkeypatch.setattr(decisions, "_approver_only", lambda inst: False)
    monkeypatch.setattr(decisions, "_is_private", lambda inst: False)
    monkeypatch.setattr("openexecutive.orchestrator.take_the_lead.room_for", lambda key, **k: state["room"])

    async def run(instance: Any, payload: Any, resolver: Any, *, by_principal: bool, grant: Any = None, **k: Any) -> Any:
        state["ran"].append(grant)
        return instance

    monkeypatch.setattr(decisions, "_run_lead", run)
    return state


def _tap(person: Any, *, allow: bool) -> Any:
    return asyncio.run(decisions.resolve_in_chat(7, person, approve=True, allow=allow))


def test_the_chat_button_allows_only_a_training_card_with_room(lead: dict[str, Any]) -> None:
    grant = {"key": "message|sam", "label": "Message Sam"}
    lead["card"] = _card({"kind": "training", "allow": grant})
    _tap(OWNER, allow=True)
    assert lead["ran"] == [grant]
    # Approve alone never allows.
    _tap(OWNER, allow=False)
    assert lead["ran"] == [grant, None]
    # A card that isn't a training one can't be allowed, grant or not.
    lead["card"] = _card({"kind": "level", "allow": grant})
    with pytest.raises(HTTPException) as refused:
        _tap(OWNER, allow=True)
    assert refused.value.status_code == 422
    # A full allowed list refuses before the action runs.
    lead["card"] = _card({"kind": "training", "allow": grant})
    lead["room"] = False
    with pytest.raises(HTTPException) as full:
        _tap(OWNER, allow=True)
    assert full.value.status_code == 409
    assert lead["ran"] == [grant, None]


def test_only_the_owners_own_tap_allows(lead: dict[str, Any]) -> None:
    lead["card"] = _card({"kind": "training", "allow": {"key": "k", "label": "Message Sam"}}, approver=2)
    with pytest.raises(HTTPException) as refused:
        _tap(TEAMMATE, allow=True)
    assert refused.value.status_code == 403
    assert lead["ran"] == []
