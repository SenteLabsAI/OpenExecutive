"""Load and validate eval scenarios.

Scenarios come from two sources:
1. Built-in YAML files shipped inside the package (`_scenarios/*.yaml`).
2. User-added scenarios stored in the `eval_scenarios` SQLite table (created
   via the `/evals/scenarios` POST endpoint). These persist on the `/data`
   volume across deploys.

Built-in scenarios are read-only. User scenarios are editable/deletable via
the API. ID collisions between the two are blocked at insert time.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from openexecutive.memory.workspace_settings import PrincipalRole


def _scenarios_dir() -> Path:
    env_path = os.environ.get("EVAL_SCENARIOS_PATH")
    if env_path:
        return Path(env_path)
    # YAMLs ship inside the package — works for any install layout.
    return Path(__file__).parent / "_scenarios"


def scenario_kind(s: dict[str, Any]) -> str:
    if s.get("domain") == "triage":
        return "triage"
    if s.get("type") == "workflow":
        return "workflow"
    if s.get("type") == "inbox":
        return "inbox"
    if s.get("requires_mcp"):
        return "mcp"
    return "chat"


def _load_builtin_scenarios() -> list[dict[str, Any]]:
    scenarios_dir = _scenarios_dir()
    if not scenarios_dir.exists():
        return []
    out = []
    for yaml_file in sorted(scenarios_dir.glob("*.yaml")):
        with open(yaml_file, encoding="utf-8") as f:
            s = yaml.safe_load(f)
        s["_kind"] = scenario_kind(s)
        s["_is_builtin"] = True
        out.append(s)
    return out


def _load_user_scenarios() -> list[dict[str, Any]]:
    # Imported lazily so this module stays usable without the DB layer
    # (e.g. the standalone `evals/run_evals.py` CLI).
    from openexecutive.evals.persistence import list_user_scenarios

    out = []
    for row in list_user_scenarios():
        try:
            s = yaml.safe_load(row["yaml"])
        except yaml.YAMLError:
            continue
        if not isinstance(s, dict):
            continue
        s["_kind"] = scenario_kind(s)
        s["_is_builtin"] = False
        out.append(s)
    return out


def load_scenarios(
    kind: str | None = None,
    scenario_id: str | None = None,
) -> list[dict[str, Any]]:
    all_scenarios = _load_builtin_scenarios() + _load_user_scenarios()

    results = []
    for s in all_scenarios:
        if scenario_id and s.get("id") != scenario_id:
            continue
        if kind and s["_kind"] != kind:
            continue
        results.append(s)
    return results


def list_scenario_meta() -> list[dict[str, Any]]:
    return [
        {
            "id": s["id"],
            "kind": s["_kind"],
            "domain": s.get("domain", ""),
            "description": s.get("description", ""),
            "workflow": s.get("workflow"),
            "is_builtin": s.get("_is_builtin", True),
        }
        for s in load_scenarios()
    ]


def scenario_principal_role(scenario: dict[str, Any]) -> PrincipalRole | None:
    """The scenario's ``principal_role`` block as a ``PrincipalRole``, or
    None when it has none. Validated like the API (``validate_role_field``);
    an unknown key, a bad value, a block with nothing in it, or a block on a
    scenario that is not ``workspace_mode: solo`` (only solo reads a role)
    raises ValueError — a typo fails the scenario instead of silently
    playing a principal with no role."""
    from openexecutive.memory.workspace_settings import (
        ROLE_FIELDS,
        PrincipalRole,
        validate_role_field,
    )

    raw = scenario.get("principal_role")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("principal_role must be a mapping")
    unknown = set(raw) - set(ROLE_FIELDS)
    if unknown:
        raise ValueError(
            f"principal_role has unknown key(s) {sorted(map(str, unknown))}; "
            f"allowed: {', '.join(ROLE_FIELDS)}"
        )
    role = PrincipalRole.model_validate({f: validate_role_field(f, raw.get(f)) for f in ROLE_FIELDS})
    if role.is_empty():
        raise ValueError("principal_role sets no field")
    if scenario.get("workspace_mode") != "solo":
        raise ValueError("principal_role needs workspace_mode: solo (only solo reads a role)")
    return role


def scenario_voice_persona(scenario: dict[str, Any]) -> str | None:
    """The scenario's ``voice_persona`` slug, or None to use the install's
    voice. An unknown slug raises ValueError, so a typo fails the scenario
    instead of silently running in the default voice."""
    from openexecutive.personas.loader import persona_exists

    slug = scenario.get("voice_persona")
    if slug is None:
        return None
    if not isinstance(slug, str) or not persona_exists(slug):
        raise ValueError(f"voice_persona {slug!r} is not a known voice")
    return slug


def scenario_delegation(scenario: dict[str, Any]) -> Any:
    """The scenario's ``delegation`` block (Act as me) as a
    ``delegation.settings.DelegationOverride`` over a fresh in-memory mailbox
    (``evals.mailbox.ScenarioMailbox``), or None when it has none.

    Shape::

        delegation:
          person: {full_name: Olivia Owner, email: olivia@fernway.example}
          thread:            # or threads: [...]
            id: t-pilot
            subject: Brand refresh pilot
            messages:
              - {from: "Dana <dana@northpeak.example>", text: "...", date: "...",
                 reply_to: "...", cc: ["..."],
                 attachments: [{name: scope.txt, text: "..."}]}

    Raises ValueError on a malformed block, so a typo fails the scenario."""
    from openexecutive.delegation.settings import DelegationOverride
    from openexecutive.evals.mailbox import ScenarioMailbox
    from openexecutive.people.models import Person

    raw = scenario.get("delegation")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("delegation must be a mapping")
    person_raw = raw.get("person")
    if not isinstance(person_raw, dict) or not person_raw.get("full_name") or "@" not in str(person_raw.get("email") or ""):
        raise ValueError("delegation.person needs full_name and email")
    email = str(person_raw["email"]).strip().lower()
    threads_raw = raw.get("threads") if raw.get("threads") is not None else (
        [raw["thread"]] if raw.get("thread") is not None else []
    )
    if not isinstance(threads_raw, list):
        raise ValueError("delegation.threads must be a list")
    threads = [_scenario_thread(t, email, "delegation") for t in threads_raw]
    return DelegationOverride(
        enabled=raw.get("enabled", True) is not False,
        gmail=ScenarioMailbox(email, threads, _scenario_files(threads_raw)),
        person=Person(id=0, full_name=str(person_raw["full_name"]), email=email, is_principal=True),
    )


def _scenario_files(threads_raw: list[Any]) -> dict[tuple[str, int], bytes]:
    """The text files attached to a ``delegation`` block's messages, by
    (message id, 1-based index) as ``_scenario_thread`` numbers them."""
    files: dict[tuple[str, int], bytes] = {}
    for t in threads_raw:
        for i, m in enumerate(t["messages"], 1):
            for n, a in enumerate(m.get("attachments") or [], 1):
                files[(f"{t['id']}-{i}", n)] = str(a.get("text") or "").encode()
    return files


def _scenario_attachments(raw: Any, block: str) -> list[Any]:
    from openexecutive.delegation.gmail import MailAttachment

    if not isinstance(raw, list) or not all(isinstance(a, dict) and a.get("name") and isinstance(a.get("text"), str) for a in raw):
        raise ValueError(f"each {block} attachment needs a name and text")
    return [
        MailAttachment(index=n, name=str(a["name"]), mime_type="text/plain", size=len(a["text"].encode()))
        for n, a in enumerate(raw, 1)
    ]


def _scenario_thread(t: Any, email: str, block: str) -> Any:
    """One thread of a ``delegation`` or ``inbox`` block as a ``MailThread``:
    ``{id, subject, messages: [{from, text, date?, cc?, reply_to?, verified?}]}``.
    A message from ``email`` is the person's own (SENT)."""
    from email.utils import getaddresses

    from openexecutive.delegation.gmail import MailMessage, MailThread, valid_id

    if not isinstance(t, dict) or not valid_id(t.get("id")) or not isinstance(t.get("messages"), list):
        raise ValueError(f"each {block} thread needs an id and a messages list")
    messages = []
    for i, m in enumerate(t["messages"], 1):
        if not isinstance(m, dict) or not m.get("from") or not isinstance(m.get("text"), str):
            raise ValueError(f"each {block} message needs from and text")
        sender = getaddresses([str(m["from"])])
        name, addr = sender[0] if sender else ("", "")
        mine = addr.strip().lower() == email
        attached = _scenario_attachments(m.get("attachments") or [], block)
        messages.append(MailMessage(
            id=f"{t['id']}-{i}",
            thread_id=str(t["id"]),
            from_addr=addr.strip().lower(),
            from_name=name.strip(),
            to=[str(a).strip().lower() for a in m.get("to") or [email]],
            cc=[str(c).strip().lower() for c in m.get("cc") or []],
            reply_to=str(m.get("reply_to") or "").strip().lower(),
            subject=str(t.get("subject") or ""),
            date=str(m.get("date") or ""),
            message_id_header=f"<{t['id']}-{i}@eval.example>",
            labels=["SENT"] if mine else ["INBOX"],
            text=m["text"],
            sender_authenticated=m.get("verified", True) is not False,
            attachments=attached,
            has_attachments=bool(attached),
        ))
    return MailThread(id=str(t["id"]), messages=messages)


def scenario_inbox(scenario: dict[str, Any]) -> Any:
    """The scenario's ``inbox`` block (the inbox watcher, ``type: inbox``) as
    an ``InboxCase``, or None when it has none. The message the watcher
    answers is the thread's newest one from someone else.

    Shape::

        inbox:
          person: {full_name: Olivia Owner, email: olivia@fernway.example}
          relation: contact          # team | contact | correspondent | stranger
          expect: draft              # draft | no_draft
          thread: {id: t1, subject: ..., messages: [...]}   # as in delegation

    Raises ValueError on a malformed block, so a typo fails the scenario."""
    from openexecutive.delegation.inbox_classifier import THRESHOLDS
    from openexecutive.people.models import Person

    raw = scenario.get("inbox")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("inbox must be a mapping")
    person_raw = raw.get("person")
    if not isinstance(person_raw, dict) or not person_raw.get("full_name") or "@" not in str(person_raw.get("email") or ""):
        raise ValueError("inbox.person needs full_name and email")
    if raw.get("relation") not in THRESHOLDS:
        raise ValueError(f"inbox.relation must be one of {sorted(THRESHOLDS)}")
    if raw.get("expect") not in ("draft", "no_draft"):
        raise ValueError("inbox.expect must be draft or no_draft")
    email = str(person_raw["email"]).strip().lower()
    thread = _scenario_thread(raw.get("thread"), email, "inbox")
    inbound = [m for m in thread.messages if m.from_addr != email]
    if not inbound:
        raise ValueError("inbox.thread needs a message from someone else")
    return InboxCase(
        person=Person(id=0, full_name=str(person_raw["full_name"]), email=email, is_principal=True),
        relation=str(raw["relation"]),
        expect_draft=raw["expect"] == "draft",
        thread=thread,
        message=inbound[-1],
    )


@dataclass
class InboxCase:
    person: Any
    relation: str
    expect_draft: bool
    thread: Any
    message: Any


def builtin_scenario_ids() -> set[str]:
    return {s["id"] for s in _load_builtin_scenarios()}


def validate_scenario_yaml(raw: str) -> dict[str, Any]:
    """Parse and lightly validate scenario YAML. Raises ValueError on problems.

    Heavy validation (workflow inputs match the workflow's input_model, etc.)
    happens at run time and surfaces as a normal `scenario_error` event.
    """
    try:
        s = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML: {e}") from e

    if not isinstance(s, dict):
        raise ValueError("Top-level YAML must be a mapping")
    if not s.get("id") or not isinstance(s["id"], str):
        raise ValueError("Missing or non-string `id` field")
    if "/" in s["id"] or " " in s["id"]:
        raise ValueError("`id` must not contain '/' or whitespace")

    if s.get("workspace_mode") is not None and s["workspace_mode"] not in ("solo", "team"):
        raise ValueError("`workspace_mode` must be 'solo' or 'team'")
    if s.get("principal_role") is not None:
        try:
            scenario_principal_role(s)
        except ValueError as e:
            raise ValueError(f"`principal_role`: {e}") from e
    if s.get("voice_persona") is not None:
        scenario_voice_persona(s)
    if s.get("delegation") is not None:
        try:
            scenario_delegation(s)
        except ValueError as e:
            raise ValueError(f"`delegation`: {e}") from e
    if s.get("inbox") is not None:
        try:
            scenario_inbox(s)
        except ValueError as e:
            raise ValueError(f"`inbox`: {e}") from e

    k = scenario_kind(s)
    if k == "chat" and not s.get("query"):
        raise ValueError("chat scenarios require a `query` field")
    if k == "workflow" and not s.get("workflow"):
        raise ValueError("workflow scenarios require a `workflow` field")
    if k == "inbox" and s.get("inbox") is None:
        raise ValueError("inbox scenarios require an `inbox` block")
    if k == "triage" and not s.get("event"):
        raise ValueError("triage scenarios require an `event` field")

    return s
