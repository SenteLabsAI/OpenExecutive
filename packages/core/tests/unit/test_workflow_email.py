"""Workflows that email as their creator (delegation/workflow_email.py):
``oe__draft_email_as_me`` drafts, ``oe__send_email_as_me`` sends, under
Act as me's rules plus the ones a send needs."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.audit import logger as audit_logger
from openexecutive.audit.logger import AuditLogger
from openexecutive.delegation import caps, handle_it, inbox, workflow_email
from openexecutive.delegation import drafts as ddrafts
from openexecutive.delegation import ghostwriter as gw
from openexecutive.delegation import settings as dsettings
from openexecutive.delegation.gmail import (
    CreatedDraft,
    DraftInfo,
    DraftSpec,
    GmailError,
    MailMessage,
    SentMessage,
)
from openexecutive.memory import episodic
from openexecutive.orchestrator.artifact_records import pinned_viewer, viewer_for_person
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.workflows import action_step, chat_tools, dynamic_store, tool_catalog
from openexecutive.workflows.designer import build_context_block

OWNER = "olivia@co.example"
TEAM = "ben@co.example"
INPUT = {"to": [TEAM], "intent": "Send Ben the Q3 revenue: up 12%."}


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    monkeypatch.setattr(audit_logger, "_default_logger", AuditLogger(db_path=path))
    monkeypatch.setattr(handle_it, "signing_ok", lambda: True)
    monkeypatch.setattr(inbox, "_client_slot_active", lambda: False)
    caps._SAVED_TODAY.clear()
    caps._IN_FLIGHT.clear()
    workflow_email._SENDING.clear()
    token = current_session.set(None)
    yield path
    current_session.reset(token)
    caps._SAVED_TODAY.clear()
    caps._IN_FLIGHT.clear()
    workflow_email._SENDING.clear()
    people_registry.invalidate()


@pytest.fixture
def people(monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    owner_id = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    teammate_id = people_store.upsert_person(full_name="Ben Teammate", role="Ops", email=TEAM)
    people_registry.invalidate()
    # Every workflow in these tests is Olivia's unless a test says otherwise.
    monkeypatch.setattr(dynamic_store, "get_owner", lambda name: owner_id)
    return owner_id, teammate_id


@pytest.fixture
def on(people: tuple[int, int]) -> int:
    owner_id, _ = people
    dsettings.set_enabled(owner_id, True, updated_by=OWNER)
    return owner_id


@pytest.fixture
def composed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_compose(**kwargs: Any) -> gw.ComposedDraft:
        calls.append(kwargs)
        return gw.ComposedDraft(subject="Q3 numbers", body="Hi Ben, revenue is up 12%.")

    monkeypatch.setattr(gw, "compose", fake_compose)
    return calls


class FakeMailbox:
    def __init__(self, opened: str = OWNER) -> None:
        self.opened = opened
        self.drafts: list[DraftSpec] = []
        self.sent: list[str] = []
        self.send_error: GmailError | None = None
        self.retarget: list[str] | None = None

    async def profile_email(self) -> str:
        return self.opened

    async def create_draft(self, spec: DraftSpec) -> CreatedDraft:
        self.drafts.append(spec)
        return CreatedDraft(draft_id="d1", message_id="m99", thread_id="tnew")

    async def get_draft(self, draft_id: str) -> DraftInfo | None:
        spec = self.drafts[-1]
        return DraftInfo(draft_id=draft_id, message=MailMessage(
            id="m99", thread_id="tnew", from_addr=OWNER, to=self.retarget or spec.to, cc=spec.cc,
        ))

    async def send_as_addresses(self) -> list[str]:
        return [OWNER]

    async def send_draft(self, draft_id: str) -> SentMessage:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(draft_id)
        return SentMessage(id="sent1", thread_id="tnew")


def _run(
    tool_input: dict[str, Any], mailbox: FakeMailbox, *, send: bool = False,
    creator: int | None = None, chain: tuple[str, ...] = ("wf",),
) -> dict[str, Any]:
    handler = workflow_email.handle_send_email_as_me if send else workflow_email.handle_draft_email_as_me

    async def go() -> str:
        outer = [chat_tools.in_workflow(name, creator) for name in chain]
        for block in outer:
            block.__enter__()
        try:
            return await handler(tool_input, mailbox=mailbox)
        finally:
            for block in reversed(outer):
                block.__exit__(None, None, None)

    return json.loads(asyncio.run(go()))


# ── Drafts ───────────────────────────────────────────────────────────────────


def test_drafts_in_the_creators_mailbox_and_returns_no_body(on, composed) -> None:
    mailbox = FakeMailbox()

    result = _run(INPUT, mailbox, creator=on)

    assert result["status"] == "drafted"
    assert result["to"] == [TEAM] and result["subject"] == "Q3 numbers"
    assert "body" not in result
    assert [d.to for d in mailbox.drafts] == [[TEAM]] and mailbox.sent == []
    assert composed[0]["intent"] == INPUT["intent"]


def test_refused_with_the_fallback_when_act_as_me_is_off(people, composed) -> None:
    owner_id, _ = people
    mailbox = FakeMailbox()

    result = _run(INPUT, mailbox, creator=owner_id)

    assert "Act as me" in result["error"] and "Executive's own email" in result["error"]
    assert mailbox.drafts == [] and composed == []


def test_refused_when_the_mailbox_is_not_theirs(on, composed) -> None:
    result = _run(INPUT, FakeMailbox(opened="someone@else.example"), creator=on)

    assert result["status"] == "mismatch" and composed == []


def test_refused_without_a_creator(people, composed) -> None:
    assert "no creator" in _run(INPUT, FakeMailbox())["error"] and composed == []


@pytest.mark.parametrize("send", [False, True])
def test_refused_when_someone_else_started_the_run(people, on, composed, send: bool) -> None:
    _, teammate_id = people
    mailbox = FakeMailbox()

    with pinned_viewer(viewer_for_person(teammate_id)):
        result = _run(INPUT, mailbox, creator=on, send=send)

    assert "someone else is behind this run" in result["error"]
    assert mailbox.drafts == [] and mailbox.sent == []


def test_refused_when_another_persons_workflow_started_it(
    people, on, composed, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, teammate_id = people
    monkeypatch.setattr(dynamic_store, "get_owner", lambda name: teammate_id if name == "bens" else on)
    mailbox = FakeMailbox()

    result = _run(INPUT, mailbox, creator=on, send=True, chain=("bens", "wf"))

    assert "someone else is behind this run" in result["error"] and mailbox.sent == []


def test_the_creator_starting_it_by_hand_may(on, composed) -> None:
    with pinned_viewer(viewer_for_person(on)):
        assert _run(INPUT, FakeMailbox(), creator=on)["status"] == "drafted"


@pytest.mark.parametrize("tool_input", [
    {"to": [TEAM]},
    {"to": [], "intent": "hi"},
    {"to": ["not-an-address"], "intent": "hi"},
    {"to": [f"p{i}@x.example" for i in range(11)], "intent": "hi"},
])
def test_bad_input_writes_nothing(on, composed, tool_input) -> None:
    mailbox = FakeMailbox()

    assert "error" in _run(tool_input, mailbox, creator=on, send=True)
    assert mailbox.drafts == [] and mailbox.sent == []


def test_a_composer_failure_is_an_error_not_a_raise(on, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(**kwargs: Any) -> gw.ComposedDraft:
        raise TimeoutError

    monkeypatch.setattr(gw, "compose", boom)
    mailbox = FakeMailbox()

    result = _run(INPUT, mailbox, creator=on, send=True)

    assert "Executive's own email" in result["error"]
    assert mailbox.drafts == [] and caps._IN_FLIGHT.get(on, 0) == 0 and workflow_email._SENDING == {}


def test_draft_limit_is_shared(on, composed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELEGATION_MAX_DRAFTS_PER_DAY", "1")
    mailbox = FakeMailbox()

    assert _run(INPUT, mailbox, creator=on)["status"] == "drafted"
    assert "limit" in _run(INPUT, mailbox, creator=on)["error"]
    assert len(mailbox.drafts) == 1


# ── Sends ────────────────────────────────────────────────────────────────────


def test_sends_from_the_creators_mailbox(on, composed) -> None:
    mailbox = FakeMailbox()

    result = _run(INPUT, mailbox, creator=on, send=True)

    assert result["status"] == "sent" and result["subject"] == "Q3 numbers"
    assert "body" not in result and "link" not in result
    assert mailbox.sent == ["d1"]
    since = ddrafts.datetime(2000, 1, 1, tzinfo=ddrafts.UTC)
    assert ddrafts.count_since(on, since, source=ddrafts.SOURCE_WORKFLOW_SENT) == 1
    assert ddrafts.sent_message_ids(on) == {"sent1"}


def test_send_limit(on, composed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELEGATION_WORKFLOW_MAX_SENDS_PER_DAY", "1")
    mailbox = FakeMailbox()

    assert _run(INPUT, mailbox, creator=on, send=True)["status"] == "sent"
    assert "limit" in _run(INPUT, mailbox, creator=on, send=True)["error"]
    assert mailbox.sent == ["d1"] and len(mailbox.drafts) == 1 and workflow_email._SENDING == {}


def test_no_send_without_signed_sign_ins(on, composed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handle_it, "signing_ok", lambda: False)
    mailbox = FakeMailbox()

    result = _run(INPUT, mailbox, creator=on, send=True)

    assert "wasn't sent" in result["error"] and "Drafts" in result["error"] and mailbox.sent == []


def test_a_draft_readdressed_before_sending_is_not_sent(on, composed) -> None:
    mailbox = FakeMailbox()
    mailbox.retarget = ["someone@else.example"]

    result = _run(INPUT, mailbox, creator=on, send=True)

    assert "wasn't sent" in result["error"] and mailbox.sent == []


def test_never_sends_to_the_executive(on, composed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "exec@co.example")
    mailbox = FakeMailbox()

    result = _run({**INPUT, "to": ["exec@co.example"]}, mailbox, creator=on, send=True)

    assert "Executive" in result["error"] and mailbox.sent == []


def test_an_unconfirmed_send_says_not_to_retry(on, composed) -> None:
    mailbox = FakeMailbox()
    mailbox.send_error = GmailError("timeout", maybe_done=True)

    result = _run(INPUT, mailbox, creator=on, send=True)

    assert "Don't try again" in result["error"] and workflow_email._SENDING == {}


# ── Wiring ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", [workflow_email.DRAFT_TOOL, workflow_email.SEND_TOOL])
def test_builtins_are_writes_and_recipients_are_targets(name: str) -> None:
    info = {t.name: t for t in tool_catalog.builtin_tools()}[name]
    assert info.read_only is None and info.source == "builtin"
    targets, complete = action_step.resource_targets({"to": [TEAM], "cc": ["c@x.example"], "intent": "x"})
    assert complete and {v for _, v in targets} == {TEAM, "c@x.example"}


def test_dispatch_writes_as_the_workflows_creator(people, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_id, _ = people
    seen: list[Any] = []

    async def handler(tool_input: dict[str, Any]) -> str:
        seen.append(chat_tools.running())
        return "{}"

    info = {t.name: t for t in tool_catalog.builtin_tools()}[workflow_email.SEND_TOOL]
    monkeypatch.setattr(tool_catalog, "builtin_handler", lambda name: handler)

    asyncio.run(action_step._dispatch(workflow_email.SEND_TOOL, {}, info, "wf"))

    assert seen == [(("wf",), owner_id)] and chat_tools.running() == ((), None)


def test_designer_is_told_whether_it_can_write_as_them(people) -> None:
    owner_id, _ = people
    assert workflow_email.available_for(owner_id) is False
    dsettings.set_enabled(owner_id, True, updated_by=OWNER)
    assert workflow_email.available_for(owner_id) is True

    yes = build_context_block(as_me=True)
    assert "their own mailbox is connected" in yes and workflow_email.SEND_TOOL in yes
    assert "Do not use oe__draft_email_as_me, oe__send_email_as_me, oe__answer_email_as_me or" in (
        build_context_block(as_me=False)
    )


# ── Saving a workflow that sends ─────────────────────────────────────────────


def _defn(tools: list[str]) -> Any:
    return SimpleNamespace(steps=[SimpleNamespace(tools=tools)])


def test_only_a_sending_workflow_is_checked_at_save(people) -> None:
    owner_id, _ = people
    nobody = SimpleNamespace(kind="open", email="")
    assert workflow_email.save_refusal(_defn([workflow_email.DRAFT_TOOL]), nobody, owner_id) is None


def test_a_sending_workflow_needs_its_verified_creator(people, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_id, teammate_id = people
    monkeypatch.setattr("openexecutive.api.caller.signing_on", lambda: True)
    defn = _defn([workflow_email.SEND_TOOL])
    olivia = SimpleNamespace(kind="user", email=OWNER)
    ben = SimpleNamespace(kind="user", email=TEAM)

    assert workflow_email.save_refusal(defn, olivia, owner_id) is None
    assert "Only the person who created" in str(workflow_email.save_refusal(defn, ben, owner_id))
    assert "Only the person who created" in str(workflow_email.save_refusal(defn, olivia, None))
    assert "Only the person who created" in str(workflow_email.save_refusal(defn, olivia, teammate_id))


def test_a_sending_workflow_needs_signed_sign_ins(people, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_id, _ = people
    monkeypatch.setattr("openexecutive.api.caller.signing_on", lambda: False)
    monkeypatch.setattr("openexecutive.utils.deployment.is_local_login", lambda: False)

    refused = workflow_email.save_refusal(
        _defn([workflow_email.SEND_TOOL]), SimpleNamespace(kind="open", email=OWNER), owner_id,
    )
    assert "signed sign-ins" in str(refused)


def test_refused_when_one_of_the_executives_own_passes_started_it(on, composed) -> None:
    """The reflection or research running the workflow: inbound text with
    nobody watching chose its inputs. A nested workflow inherits that."""
    mailbox = FakeMailbox()
    token = current_session.set(SimpleNamespace(unattended=True))
    try:
        result = _run(INPUT, mailbox, creator=on, send=True, chain=("wf", "inner"))
    finally:
        current_session.reset(token)

    assert "someone else is behind this run" in result["error"] and mailbox.sent == []
    assert chat_tools.started_by_a_pass() is False


def test_chat_cannot_replace_a_workflow_that_sends(people, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import workflow_authoring_tools as wat

    existing = SimpleNamespace(is_active=False, steps=[SimpleNamespace(tools=[workflow_email.SEND_TOOL])])
    monkeypatch.setattr(dynamic_store, "get_definition", lambda name: existing)
    monkeypatch.setattr(wat, "_canonical_token", lambda definition: "tok")
    saved: list[Any] = []
    monkeypatch.setattr(dynamic_store, "save_if_unchanged", lambda *a, **k: saved.append(a))
    definition = {
        "name": "weekly_update", "title": "Weekly update",
        "steps": [
            {"kind": "specialist", "id": "numbers", "title": "S", "specialist": "cfo", "goal": "Numbers."},
            {"kind": "synthesis", "id": "assemble", "title": "A"},
        ],
    }

    out = json.loads(asyncio.run(wat.handle_save_workflow(
        {"definition": definition, "confirm_token": "tok", "overwrite": True},
    )))

    assert "can't be replaced from chat" in out["error"] and saved == []


def test_a_step_timeout_cannot_cut_a_send_short(on, composed) -> None:
    # The step's wait_for cancels the call mid-send; the send still finishes,
    # is recorded, and frees the send slot.
    mailbox = FakeMailbox()
    sending = asyncio.Event()
    release = asyncio.Event()
    plain_send = mailbox.send_draft

    async def slow_send(draft_id: str) -> SentMessage:
        sending.set()
        await release.wait()
        return await plain_send(draft_id)

    mailbox.send_draft = slow_send  # type: ignore[method-assign]

    async def go() -> None:
        with chat_tools.in_workflow("wf", on):
            call = asyncio.ensure_future(workflow_email.handle_send_email_as_me(INPUT, mailbox=mailbox))
            await sending.wait()
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            release.set()
            for _ in range(20):
                await asyncio.sleep(0)

    asyncio.run(go())

    assert mailbox.sent == ["d1"] and workflow_email._SENDING == {}
    assert ddrafts.sent_message_ids(on) == {"sent1"}


def test_email_tools_get_more_than_the_default_step_timeout() -> None:
    tools = {t.name: t for t in tool_catalog.builtin_tools()}
    for name in workflow_email.TOOLS:
        assert (tools[name].timeout_s or 0) > action_step._TOOL_CALL_TIMEOUT_S


def test_chat_cannot_add_a_send_to_someone_elses_workflow(people, monkeypatch: pytest.MonkeyPatch) -> None:
    # Saved over an existing workflow, the send would go as its creator,
    # not as whoever asked in chat.
    from openexecutive.orchestrator import workflow_authoring_tools as wat

    existing = SimpleNamespace(is_active=False, steps=[SimpleNamespace(tools=[])])
    incoming = SimpleNamespace(name="weekly_update", steps=[SimpleNamespace(tools=[workflow_email.SEND_TOOL])])
    monkeypatch.setattr(dynamic_store, "get_definition", lambda name: existing)

    async def build(definition: Any) -> tuple[Any, None]:
        return incoming, None

    monkeypatch.setattr(wat, "_build_def", build)
    monkeypatch.setattr(wat, "_canonical_token", lambda definition: "tok")
    monkeypatch.setattr(wat, "_approved_tool_workflow_error", lambda name, existing: None)
    saved: list[Any] = []
    monkeypatch.setattr(dynamic_store, "save_if_unchanged", lambda *a, **k: saved.append(a))

    out = json.loads(asyncio.run(wat.handle_save_workflow(
        {"definition": {"name": "weekly_update"}, "confirm_token": "tok", "overwrite": True},
    )))

    assert "can't be replaced from chat" in out["error"] and saved == []


# ── Who an email goes to without asking ──────────────────────────────────────


def _saved(goal: str, tools: list[str], *, read_goal: str = "Read the inbox") -> Any:
    from openexecutive.workflows.dynamic_models import DynamicWorkflowDef

    return DynamicWorkflowDef.model_validate({
        "name": "weekly_update", "title": "Weekly update",
        "steps": [
            {"kind": "action", "id": "s0", "title": "Read", "goal": read_goal, "tools": ["oe__search_my_email"]},
            {"kind": "action", "id": "s1", "title": "Write", "goal": goal, "tools": tools},
            {"kind": "synthesis", "id": "s2", "title": "Report", "instructions": "Never copy cfo@co.example"},
        ],
    })


def _send_policy(monkeypatch: pytest.MonkeyPatch, defn: Any, stored: Any) -> action_step.TargetPolicy:
    from openexecutive.workflows import approved_targets

    monkeypatch.setattr(dynamic_store, "get_definition", lambda name: stored)
    monkeypatch.setattr(approved_targets, "approved_values", lambda name, db_path=None: set())
    monkeypatch.setattr(action_step, "approved_values", lambda name, db_path=None: set())
    return action_step.TargetPolicy.for_workflow(defn)


def test_an_email_to_someone_they_chose_goes_without_asking(people, monkeypatch) -> None:
    owner_id, _ = people
    people_store.add_person_email(owner_id, "liv@co.example")
    defn = _saved(f"Email the summary to Ben.Teammate@co.example and\n{TEAM}", [workflow_email.SEND_TOOL])

    policy = _send_policy(monkeypatch, defn, defn)
    targets = [("to", "ben.teammate@co.example"), ("to", TEAM), ("cc", OWNER.upper()), ("cc", "liv@co.example")]

    assert policy.unapproved(targets) == []
    # Mail a step read can't add a recipient that goes unasked.
    assert policy.unapproved([("to", "eve@evil.example")]) == [("to", "eve@evil.example")]


def test_only_the_goal_of_a_step_that_emails_names_who(people, monkeypatch) -> None:
    defn = _saved(f"Email it to {TEAM}", [workflow_email.SEND_TOOL], read_goal="Skip anything from ceo@co.example")

    policy = _send_policy(monkeypatch, defn, defn)

    assert policy.unapproved([("to", TEAM)]) == []
    for elsewhere in ("ceo@co.example", "cfo@co.example"):
        assert policy.unapproved([("to", elsewhere)]) == [("to", elsewhere)]


def test_only_the_saved_copy_of_a_workflow_that_writes_as_them_names_who(people, monkeypatch) -> None:
    sends = _saved(f"Email it to {TEAM}", [workflow_email.SEND_TOOL])
    # A workflow that doesn't write as its creator: anyone may edit its text.
    plain = _saved(f"Email it to {TEAM}", ["google_workspace__send_gmail_message"])
    assert _send_policy(monkeypatch, plain, plain).unapproved([("to", TEAM)]) == [("to", TEAM)]
    # The definition running isn't the one its creator saved.
    edited = _saved("Email it to eve@evil.example", [workflow_email.SEND_TOOL])
    policy = _send_policy(monkeypatch, edited, sends)
    assert policy.unapproved([("to", "eve@evil.example")]) == [("to", "eve@evil.example")]
    assert policy.unapproved([("to", TEAM)]) == [("to", TEAM)]
    # Not saved at all.
    assert _send_policy(monkeypatch, sends, None).unapproved([("to", TEAM)]) == [("to", TEAM)]


def test_chosen_recipients_never_raises(people, monkeypatch) -> None:
    def boom(name: str) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(dynamic_store, "get_definition", boom)
    assert workflow_email.chosen_recipients(_saved(f"to {TEAM}", [workflow_email.SEND_TOOL])) == set()
